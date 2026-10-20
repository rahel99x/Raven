"""Real retained-data export through the shared background and terminal controls."""
from __future__ import annotations

import copy
import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven import data_export_ui as U, layout as L, launch_settings, projects, reporting
from raven.config import Config
from raven.controller import App
from raven.model import Store
from raven.remote import LocalFiles


class Background:
    demo = False
    closed = False
    future = None

    def __init__(self):
        self.pending = None
        self.files = LocalFiles()
        self.settings = {}

    def start_task(self, work, complete):
        if self.pending:
            return False
        self.pending = work, complete
        return True

    def finish(self):
        work, complete = self.pending
        self.pending = None
        try:
            value = work()
        except Exception as exc:
            value = exc
        complete(value)


@pytest.fixture
def app(tmp_path):
    (tmp_path / 'train.py').write_text('print("science")\n')
    run = reporting.begin_run(tmp_path, 'fit-a1', name='Fit alpha', script='train.py', job_id='1170')
    reporting.write_metric(run, {'loss': 0.125, 'accuracy': 0.9}, t=1700000000.125, step=1)
    reporting.write_metric(run, {'loss': 0.025, 'accuracy': 0.95}, t=1700000001.125, step=2)
    reporting.finish_run(run, state='COMPLETED', runtime_seconds=2)
    value = App(Store(persist=False), None, None, Config({}), 'export-tester')
    value.research = Background()
    value.files = value.research.files
    value.width, value.height = 90, 30
    value.tab = 'research'
    value.project_state.update(root=str(tmp_path), registered_root=str(tmp_path), binding=projects.select_run(str(tmp_path), 'fit-a1')['binding'])
    value.test_root, value.test_run = tmp_path, run
    return value


def render(app, ascii_=False):
    return U.overlay(SimpleNamespace(g=L.Glyphs(ascii_)), {}, app, app.width, app.height)


def open_ready(app):
    assert U.open_export(app)
    assert app.data_export_state['stage'] == 'busy'
    app.research.finish()
    assert app.data_export_state['stage'] == 'form', app.data_export_state['error']
    return app.data_export_state


def command(app, action):
    U.run_command(app, ['exportdata', 'action', action])


def click(app, action):
    choices = [key for _, key in U._document(app) if key]
    app.data_export_state.update(cursor=choices.index(action), follow=True)
    render(app)
    hit = next(item for item in app.data_export_state['control_hits'] if item[2]['action'][1].endswith(action))
    y, _, item = hit
    U.handle_mouse(app, y, item['left'])


def test_real_manual_export_frozen_attempt_and_single_metric(app):
    state = open_ready(app)
    command(app, 'none')
    command(app, 'family:application')
    command(app, 'field:metric_name')
    U.paste(app, 'loss')
    U.handle_key(app, 'enter')
    # Completion, cursor movement, and an entirely different selected project
    # cannot retarget this already opened panel.
    app.project_state['binding'] = {'project_root': '/wrong', 'run_id': 'different'}
    app.selected_id = app.research_job_id = '9999'
    click(app, 'review')
    app.research.finish()
    assert state['stage'] == 'review', state['error']
    plan = copy.deepcopy(state['plan'])
    assert plan['request']['attempts'][0]['run_id'] == 'fit-a1'
    assert not (app.test_root / 'exports').exists()
    click(app, 'export')
    app.research.finish()
    receipt = state['receipt']
    assert receipt['status'] == 'complete', receipt
    output = Path(receipt['outputs'][0])
    rows = list(csv.DictReader((output / 'application-metrics.csv').open()))
    assert {row['metric'] for row in rows} == {'loss'}
    assert [float(row['value']) for row in rows] == [0.125, 0.025]
    assert state['attempts'][0]['project'] == str(app.test_root)
    assert (output / 'manifest.json').is_file()


@pytest.mark.parametrize('tab', ['jobs', 'history', 'research', 'analytics'])
def test_entry_points_keep_exact_bound_run(app, tab):
    app.tab = tab
    state = open_ready(app)
    assert state['attempts'][0]['run_id'] == 'fit-a1'
    assert state['attempts'][0]['project'] == str(app.test_root)


def test_only_admitted_collapsed_members_are_frozen(app, monkeypatch):
    from raven.ui import selection_state
    monkeypatch.setattr(selection_state, 'context', lambda app: {'scope': 'jobs', 'ids': ('1', '3'), 'selected': '1'})
    app.tab, app.selected_id = 'jobs', '1'
    app.job_group_metadata = {'jobs': {'1': SimpleNamespace(header=True, collapsed=True,
                                records=(SimpleNamespace(id='1'), SimpleNamespace(id='2')))}}
    assert U._selected_jobs(app) == ('1', '2')
    app.marks = {'3'}
    assert U._selected_jobs(app) == ('3',)
    app.marks = {'outside'}
    with pytest.raises(ValueError, match='outside'):
        U._selected_jobs(app)


def test_paint_and_navigation_never_read_source_or_query_scheduler(app, monkeypatch):
    state = open_ready(app)
    import builtins
    monkeypatch.setattr(builtins, 'open', lambda *a, **kw: pytest.fail('paint attempted file IO'))
    monkeypatch.setattr(Path, 'open', lambda *a, **kw: pytest.fail('paint attempted pathlib IO'))
    for _ in range(25):
        render(app)
        U.handle_key(app, 'down')
    assert state['attempts'][0]['run_id'] == 'fit-a1'


@pytest.mark.parametrize('width,height', [(12, 5), (24, 8), (40, 12), (80, 24), (120, 40)])
@pytest.mark.parametrize('ascii_', [False, True])
def test_narrow_ascii_all_choices_remain_keyboard_reachable(app, width, height, ascii_):
    state = open_ready(app)
    app.width, app.height = width, height
    choices = [key for _, key in U._document(app) if key]
    seen = set()
    for _ in range(len(choices)):
        rows = render(app, ascii_)
        assert all(L.vlen(L.row_text(row)) <= width for _, _, row in rows)
        seen.add(choices[state['cursor']])
        U.handle_key(app, 'tab')
    assert {'review', 'save', 'save-preset', 'future-auto', 'destination'} <= seen


def test_stale_pointer_does_not_select_another_action(app):
    state = open_ready(app)
    render(app)
    y, _, item = state['control_hits'][1]
    U.handle_key(app, 'down')
    U.handle_mouse(app, y, item['left'])
    assert state['stage'] == 'form'
    assert not app.research.pending


def test_cancel_and_stale_completion_do_not_replace_new_panel(app):
    assert U.open_export(app)
    work, finished = app.research.pending
    U.cancel(app, close=True)
    app.research.pending = None
    open_ready(app)
    current = app.data_export_state['discovery']
    finished(ValueError('late failure from previous panel'))
    assert app.data_export_state['discovery'] is current
    assert not app.data_export_state['error']


def test_failure_preserves_selection_and_full_notification(app, monkeypatch):
    state = open_ready(app)
    state.update(include=['application'], metrics=['loss'])
    state['destination'] = dict(mode='relative', path='figures/α data', host='')
    from raven import data_export
    long_error = 'Destination rejected: ' + 'permission detail ' * 800
    monkeypatch.setattr(data_export, 'plan_export', lambda request: (_ for _ in ()).throw(OSError(long_error)))
    command(app, 'review')
    app.research.finish()
    assert state['error'] == long_error
    assert state['metrics'] == ['loss']
    assert state['destination']['path'] == 'figures/α data'
    notices = app.notifications.snapshot().notices
    assert any(long_error in item.text for item in notices)


def test_project_preferences_named_presets_have_no_job_ids(app):
    state = open_ready(app)
    state.update(include=['application'], metrics=['loss'], layout='metric', future_auto=True)
    state['destination'] = dict(mode='relative', path='figures/α data', host='')
    command(app, 'save-preset')
    app.research.finish()
    assert not state['error'], state['error']
    settings = launch_settings.load_settings(app.test_root)
    assert settings['default_export_preset'] == 'Paper figures'
    assert settings['export_preferences']['metrics'] == ['loss']
    assert settings['launch']['auto_export']['layout'] == 'metric'
    assert 'fit-a1' not in json.dumps(settings)
    U.cancel(app, close=True)
    state = open_ready(app)
    assert state['metrics'] == ['loss']
    assert state['layout'] == 'metric'
    assert state['future_auto']


def test_absolute_preference_cannot_move_between_hosts_or_users(app):
    state = open_ready(app)
    U._apply_preferences(state, {'destination': {'mode': 'absolute', 'path': '/cluster/alice', 'host': 'another-host', 'user': 'alice'}})
    assert state['destination']['mode'] == 'absolute'
    assert state['destination']['path'] == '/cluster/alice'
    with pytest.raises(ValueError, match='another host or user'):
        U.request(app)
    command(app, 'destination')
    assert U.request(app)['destination']['mode'] == 'project'
    assert 'another host or user' in state['message']


def test_completion_request_saved_without_manual_export(app):
    from raven import auto_export
    state = open_ready(app)
    state.update(include=['application'], auto=True)
    command(app, 'review')
    app.research.finish()
    command(app, 'schedule')
    app.research.finish()
    assert state['receipt'] is not None, state['error']
    assert state['receipt']['status'] == 'pending', state['error']
    statuses = auto_export.read_status(app.test_run)
    assert len(statuses) == 1
    assert statuses[0]['status'] == 'pending'
    assert not (app.test_root / 'exports').exists()


def test_remote_source_never_falls_back_to_local_same_path(app, monkeypatch):
    app.files = SimpleNamespace(remote=True, host='cluster.example')
    monkeypatch.setattr(projects, 'select_run', lambda *a, **kw: pytest.fail('remote source used local filesystem'))
    assert U.open_export(app)
    assert 'cluster.example' in app.data_export_state['error']
    assert app.research.pending is None


def test_launch_setup_saves_declarations_and_clears_binding_without_submission(app):
    from raven import launch_setup_ui as S
    assert S.open_setup(app)
    app.research.finish()
    state = app.data_export_state
    assert state['stage'] == 'setup'
    for key, value in [('application_metrics', 'native.jsonl'), ('workers', '8'), ('problem_size', '1000'), ('repeat', '2'), ('artifact', 'results/model.bin')]:
        S.apply_field(app, key, value)
    S.activate(app, 'setup:feature:experiment')
    S.activate(app, 'setup:toggle:gpu')
    S.save(app)
    app.research.finish()
    assert not state['error'], state['error']
    launch = launch_settings.load_settings(app.test_root)['launch']
    assert launch['application_metrics'] == 'native.jsonl'
    assert launch['scientific_context'] == {'workers': 8, 'problem_size': 1000, 'repeat': 2}
    assert launch['artifacts'] == ['results/model.bin']
    S.apply_field(app, 'application_metrics', '')
    S.save(app)
    app.research.finish()
    assert 'application_metrics' not in launch_settings.load_settings(app.test_root)['launch']
    assert state['stage'] == 'setup'


def test_launch_setup_rejects_escaping_path_and_keeps_form(app):
    from raven import launch_setup_ui as S
    S.open_setup(app)
    app.research.finish()
    S.apply_field(app, 'application_metrics', '../outside.jsonl')
    S.save(app)
    app.research.finish()
    assert app.data_export_state['stage'] == 'setup'
    assert 'project-relative' in app.data_export_state['error']
    assert app.data_export_state['setup_fields']['application_metrics'] == '../outside.jsonl'


@pytest.mark.parametrize('ascii_', [False, True])
@pytest.mark.parametrize('width', [24, 80, 120])
def test_export_button_and_setup_actions_available_on_all_views(app, ascii_, width):
    from raven import toolbar
    for tab in ('jobs', 'history', 'research', 'analytics'):
        app.tab = tab
        row = toolbar.render_bar(SimpleNamespace(g=L.Glyphs(ascii_)), app, width)
        assert L.vlen(L.row_text(row)) == width
        assert not any(hit[3] == 'export-data' for hit in app.toolbar_state['hits'])
        if width >= 24:
            assert 'Launch' in L.row_text(row)
        assert any(item.command == 'exportdata' for item in toolbar.menu_items(app, 'File'))
    assert any(item.command == 'launchsetup' for item in toolbar.menu_items(app, 'Setup'))


def test_chart_shortcut_exports_exact_metric_original_range_and_restores_modal(app):
    from raven import chart_interaction as C, job_selection
    from raven.interaction import Rect
    key = ('reported-metric', '1170', 'loss', str(app.test_run / 'metrics.jsonl'),
           1, str(app.test_root), 'fit-a1', 1)
    plot = C.Plot(key, Rect(5, 5, 12, 60), Rect(5, 5, 12, 60),
                  (1700000000.5, 1700000001.5), (0, 1))
    app.chart_interaction_state.update(plots=(plot,), hovered=key, last_key=key)
    app.mode = 'analysis'
    # Native event routing must preserve Shift before ordinary right-click
    # chart reset/selection clearing sees the event.
    original = C._at
    C._at = lambda *args, **kwargs: plot
    try:
        assert job_selection.context_click(app, 7, 7, button='right', shift=True)
    finally:
        C._at = original
    app.research.finish()
    state = app.data_export_state
    assert state['include'] == ['application']
    assert state['metrics'] == ['loss']
    assert U.request(app)['start'] == 1700000000.5
    assert U.request(app)['end'] == 1700000001.5
    assert state['return_mode'] == 'analysis'
    U.handle_key(app, 'esc')
    assert app.mode == 'analysis'


def test_chart_with_elapsed_axis_does_not_invent_epoch_range(app):
    from raven import chart_interaction as C
    from raven.interaction import Rect
    key = ('scientific-scaling', '1170', 'loss', str(app.test_run / 'metrics.jsonl'),
           1, str(app.test_root), 'fit-a1', 1)
    app.chart_interaction_state.update(plots=(C.Plot(key, Rect(1, 1, 5, 40), Rect(1, 1, 5, 40), (0, 60), (0, 1)),), last_key=key)
    U.open_export(app, chart=True)
    app.research.finish()
    assert app.data_export_state['range_policy'] == 'full'
    assert 'no recorded wall-clock range' in app.data_export_state['message']
    assert 'start' not in U.request(app)


def test_keyboard_focus_controls_and_mouse_use_same_commands(app):
    from raven import interaction
    state = open_ready(app)
    overlay = render(app)
    # Published overlay controls expose wrapped labels and exact actions for
    # F8 access; they do not depend on private click coordinates.
    controls = tuple(interaction._modal_controls(app, overlay, app.width, app.height, ()))
    assert any(control.label == 'Review export destination' for control in controls)
    assert all(control.action[0] == 'command' for control in controls)
    choices = [action for _, action in U._document(app) if action]
    state['cursor'] = choices.index('layout')
    U.handle_key(app, 'enter')
    assert state['layout'] == 'metric'
    click(app, 'layout')
    assert state['layout'] == 'category'


def test_connection_switch_cancels_worker_and_discards_published_metadata(app):
    U.open_export(app)
    old = app.research
    app.research = Background()
    U.tick(app)
    assert app.data_export_state['cancel'].is_set()
    old.finish()
    assert not app.data_export_state['attempts']
    assert 'connection changed' in app.data_export_state['error']


def test_export_completed_before_close_still_reports_actual_saved_path(app, monkeypatch):
    state = open_ready(app)
    state['include'] = ['application']
    command(app, 'review')
    app.research.finish()
    command(app, 'export')
    _, callback = app.research.pending
    U.cancel(app, close=True)
    callback(dict(status='complete', outputs=['/actual/export/path'], files=2, attempts=1))
    assert '/actual/export/path' in app.message
    assert app.mode == 'main'


def test_launch_setup_long_form_is_reachable_at_narrow_width(app):
    from raven import launch_setup_ui as S
    S.open_setup(app)
    app.research.finish()
    S.activate(app, 'setup:toggle:trace')
    app.width, app.height = 24, 7
    state = app.data_export_state
    choices = [key for _, key in U._document(app) if key]
    seen = set()
    for _ in range(len(choices)):
        render(app, True)
        seen.add(choices[state['cursor']])
        U.handle_key(app, 'tab')
    assert {'setup:save', 'setup:recipe-save', 'setup:adapter', 'setup:feature:scaling'} <= seen


def test_managed_light_uses_real_shared_worker_without_heavy_or_scheduler(app):
    from raven.research import ResearchHub
    from raven.raven_execution import ExecutionRouter
    cfg = Config({'raven': {'managed': True, 'mode': 'light', 'site': 'desktop'}})
    hub = ResearchHub(cfg, files=app.files)
    hub.execution = ExecutionRouter(cfg['raven'], lambda: pytest.fail('export requested Heavy'))
    app.research = hub
    try:
        U.open_export(app)
        hub.future.result(timeout=5)
        hub.poll_task()
        assert app.data_export_state['stage'] == 'form', app.data_export_state['error']
        app.data_export_state['include'] = ['application']
        command(app, 'review')
        hub.future.result(timeout=5)
        hub.poll_task()
        command(app, 'export')
        hub.future.result(timeout=5)
        hub.poll_task()
        assert app.data_export_state['receipt']['status'] == 'complete', app.data_export_state['error']
    finally:
        hub.close()


def test_indexed_job_resolution_rejects_recycled_id_without_scanning_project(app, monkeypatch):
    from threading import Event
    run = json.loads((app.test_run / 'run.json').read_text())
    run['submit'] = 1700000000
    found = {'root': str(app.test_root), 'runs': [{'run_id': 'fit-a1', 'inventory': run}]}
    monkeypatch.setattr(projects, '_indexed_project', lambda *a: found)
    monkeypatch.setattr(projects, 'discover_project', lambda *a, **kw: pytest.fail('indexed export scanned all runs'))
    frozen = ({'job_id': '1170', 'cluster': '', 'restart': None, 'submit': '2026-10-12T00:00:00',
               'roots': (str(app.test_root),), 'host': U._host(app)},)
    with pytest.raises(ValueError, match='0 matching indexed attempts'):
        U._resolve(frozen, Event())


def test_future_completion_defaults_refuse_previous_job_time_range(app):
    state = open_ready(app)
    state.update(future_auto=True, range_policy='explicit', start='1700000000', end='1700000001')
    command(app, 'save')
    assert 'Full recorded run' in state['error']
    assert not app.research.pending
    assert not (app.test_root / '.raven' / 'automation.json').exists()


def test_newly_discovered_sources_are_opt_in_and_missing_families_stay_visible(app):
    state = open_ready(app)
    assert state['include'] == ['application', 'summary']
    labels = [label for label, _ in U._document(app)]
    assert any('GPU measurements - unavailable:' in label for label in labels)
    assert any('[ ] Logs' in label for label in labels)


def test_unchanged_paint_and_navigation_reuse_prepared_document(app, monkeypatch):
    from raven import notification_ui
    state = open_ready(app)
    render(app)
    cached_document = state['cache']['document']
    monkeypatch.setattr(U, '_build_document', lambda app: pytest.fail('navigation rebuilt semantic metadata'))
    monkeypatch.setattr(notification_ui, '_wrap', lambda *a, **kw: pytest.fail('navigation reflowed all metadata'))
    for _ in range(20):
        U.handle_key(app, 'down')
        render(app)
        U.handle_mouse(app, 3, 3, button='wheel-down')
        render(app)
    assert state['cache']['document'] is cached_document


@pytest.mark.parametrize('operation', ['save', 'save-preset', 'schedule'])
def test_cancel_before_worker_start_never_writes_preferences_or_schedule(app, operation):
    from raven import auto_export
    state = open_ready(app)
    if operation == 'schedule':
        state.update(include=['application'], auto=True)
        command(app, 'review')
        app.research.finish()
    command(app, operation)
    U.cancel(app, close=True)
    app.research.finish()
    assert not (app.test_root / '.raven' / 'automation.json').exists()
    assert auto_export.read_status(app.test_run) == []
    assert not (app.test_root / 'exports').exists()


def test_unrelated_presets_survive_concurrent_save(app, monkeypatch):
    state = open_ready(app)
    launch_settings.update_settings(app.test_root, {'export_presets': {'Other': {'layout': 'category'}}})
    original = launch_settings.update_settings
    changes = []
    def save(project, update, **kwargs):
        changes.append(copy.deepcopy(update))
        original(project, {'export_presets': {'Other': {'layout': 'metric'}}})
        return original(project, update, **kwargs)
    monkeypatch.setattr(launch_settings, 'update_settings', save)
    command(app, 'save-preset')
    app.research.finish()
    assert not state['error'], state['error']
    assert set(changes[0]['export_presets']) == {'Paper figures'}
    assert launch_settings.load_settings(app.test_root)['export_presets']['Other']['layout'] == 'metric'


@pytest.mark.parametrize('device', ['0', 'node-a/nvidia/MIG-GPU-abc/1/2/partition-0'])
def test_chart_device_identity_is_never_broadened_or_truncated(app, device):
    from raven import chart_interaction as C
    from raven.interaction import Rect
    key = ('reported-metric', '1170', 'gpu-trace:' + device + ':busy-mean', 'gpu',
           1, str(app.test_root), 'fit-a1', 1)
    app.chart_interaction_state.update(plots=(C.Plot(key, Rect(1, 1, 5, 40), Rect(1, 1, 5, 40), (1700000000, 1700000001), (0, 1)),), last_key=key)
    U.open_export(app, chart=True)
    app.research.finish()
    assert U.request(app)['devices'] == [device]
    assert U.request(app)['include'] == ['gpu']
    assert 'original utilization observations' in app.data_export_state['message']


def test_small_valid_epoch_chart_is_supported_but_large_scientific_axis_is_not(app):
    from raven import chart_interaction as C
    from raven.interaction import Rect
    plot = C.Plot(('reported-metric', '1170'), Rect(1, 1, 5, 40), Rect(1, 1, 5, 40), (0, 60), (0, 1))
    app.chart_interaction_state.update(plots=(plot,), last_key=plot.key)
    assert U._chart_range(app) == (0, 60)
    from dataclasses import replace
    plot = replace(plot, key=('scientific-scaling', '1170'), x_bounds=(10**12, 10**13))
    app.chart_interaction_state.update(plots=(plot,), last_key=plot.key)
    assert U._chart_range(app) is None


def test_saved_relative_destination_follows_project_host_and_absolute_captures_user(app):
    import getpass, socket
    state = open_ready(app)
    U._apply_preferences(state, {'destination': {'mode': 'relative', 'path': 'figures/data', 'host': 'old-workstation'}})
    assert U.request(app)['destination']['host'] == ''
    state.update(future_auto=True)
    state['destination'] = {'mode': 'absolute', 'path': str(app.test_root / 'private-exports'), 'host': socket.gethostname()}
    command(app, 'save')
    app.research.finish()
    saved = launch_settings.load_settings(app.test_root)
    assert saved['launch']['auto_export_scope'] == {'host': socket.gethostname(), 'user': getpass.getuser()}
    assert 'user' not in saved['launch']['auto_export']['destination']


def test_launch_setup_native_report_binding_is_saved_and_decimal_size_is_valid(app):
    from raven import launch_setup_ui as S
    S.open_setup(app)
    app.research.finish()
    S.apply_field(app, 'problem_size', '.1')
    S.apply_field(app, 'report:scaling', 'reports/scaling.json')
    S.save(app)
    app.research.finish()
    assert not app.data_export_state['error'], app.data_export_state['error']
    launch = launch_settings.load_settings(app.test_root)['launch']
    assert launch['research_sources'] == {'scaling': 'reports/scaling.json'}
    assert launch['scientific_context']['problem_size'] == .1


def test_research_run_ignores_marks_from_an_unfocused_job_list(app):
    app.marks = {'unrelated-job'}
    state = open_ready(app)
    assert [item['run_id'] for item in state['attempts']] == ['fit-a1']


def test_explicit_run_picker_selection_overrides_stale_job_cursor(app):
    app.mode, app.selected_id = 'project_runs', 'old-job'
    app.marks = {'unrelated-job'}
    state = open_ready(app)
    assert [item['run_id'] for item in state['attempts']] == ['fit-a1']


def test_cached_binding_cannot_follow_a_recycled_job_identity(app):
    from threading import Event
    binding = copy.deepcopy(app.project_state['binding'])
    with pytest.raises(ValueError, match='submission differs'):
        U._resolve(({'binding': binding, 'host': U._host(app), 'submit': '2026-10-12T00:00:00'},), Event())
    with pytest.raises(ValueError, match='restart differs'):
        U._resolve(({'binding': binding, 'host': U._host(app), 'restart': 2},), Event())


def test_keyboard_path_spaces_and_unicode_survive_without_shell_expansion(app):
    state = open_ready(app)
    command(app, 'destination')
    command(app, 'field:path')
    for key in ['α', 'space', '$', '(', 'x', ')']:
        U.handle_key(app, key)
    U.handle_key(app, 'enter')
    assert U.request(app)['destination']['path'] == 'α $(x)'
    command(app, 'review')
    app.research.finish()
    assert state['stage'] == 'review'
    assert 'α $(x)' in state['plan']['destinations'][0]['path']
    assert not (app.test_root / 'α $(x)').exists()


def test_long_notification_metadata_materializes_only_visible_rows(app, monkeypatch):
    from raven.notification_ui import Wrapped
    state = open_ready(app)
    state['error'] = ('complete diagnostic word ' * 20000) + 'END-OF-DIAGNOSTIC'
    U._invalidate(state)
    calls = []
    original = Wrapped.line
    monkeypatch.setattr(Wrapped, 'line', lambda self, index: (calls.append(index), original(self, index))[1])
    app.width, app.height = 24, 8
    render(app)
    assert len(calls) <= app.height
    assert len(state['cache']['document']) < 100
    assert state['cache']['total'] > 10000
    calls.clear()
    U.handle_key(app, 'pgdn')
    render(app)
    assert len(calls) <= app.height
    assert state['error'].endswith('END-OF-DIAGNOSTIC')


def test_explicit_absolute_combined_bundle_control_keeps_both_projects(app, tmp_path):
    state = open_ready(app)
    second = tmp_path / 'project-two'
    second.mkdir()
    (second / 'train.py').write_text('print("another run")\n')
    run = reporting.begin_run(second, 'fit-a1', name='second', script='train.py')
    reporting.write_metric(run, {'loss': .2}, t=1700000001)
    state['attempts'] += ({'project': str(second), 'run_id': 'fit-a1', 'host': U._host(app)},)
    state['include'] = ['application']
    state['destination'] = {'mode': 'absolute', 'path': str(tmp_path / 'both-projects'), 'host': ''}
    U._invalidate(state)
    assert any(action == 'combined' for _, action in U._document(app))
    command(app, 'combined')
    command(app, 'review')
    app.research.finish()
    assert state['stage'] == 'review', state['error']
    assert len(state['plan']['destinations']) == 1
    assert {item['project'] for item in state['plan']['attempts']} == {str(tmp_path), str(second)}
    assert not (tmp_path / 'both-projects').exists()


def test_external_all_preset_selects_available_families_without_implicit_logs(app):
    state = open_ready(app)
    state['presets']['Every available metric'] = {'include': ['all'], 'layout': 'category'}
    command(app, 'preset:Every available metric')
    assert state['include'] == ['application', 'summary']
    command(app, 'review')
    app.research.finish()
    assert state['stage'] == 'review', state['error']
    assert state['plan']['valid']
