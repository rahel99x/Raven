"""Cross-feature behavior for table state, activity, raw exports and input."""
import copy
import curses

import pytest

from raven import activity_ui, log_copy, screen, table_ui, workbench
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs, row_text
from raven.model import Finished, Job, Store
from raven.views import Views

@pytest.fixture(autouse=True)
def durable_export_destination(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_EXPORT_DIR", str(tmp_path / "state" / "exports"))



@pytest.fixture
def dashboard(tmp_path):
    cfg = Config({'clipboard': {'tools': False, 'osc52': False}, 'log_lines': 0})
    store = Store(state_dir=str(tmp_path / 'state'))
    store.apply_jobs([Job('101_0', 'array-zero', 'gpu', 'RUNNING'),
                      Job('101_1', 'array-one', 'gpu', 'RUNNING'),
                      Job('201', 'waiting', 'main', 'PENDING')])
    store.apply_finished([Finished('301', 'failed', 'FAILED'),
                          Finished('302', 'success', 'COMPLETED')])
    app = App(store, None, None, cfg, 'reviewer')
    views = Views(Glyphs(False), cfg)
    app.views_ref = views
    views.compose(store.snapshot(), app, 150, 30)
    return app, views, store


def test_saved_views_restore_complete_settings_and_keep_tables_independent(dashboard):
    app, views, store = dashboard
    app.run_command('facet jobs state=RUNNING partition=gpu')
    app.run_command('columns jobs hide gpu')
    app.filter, app.sort['jobs'], app.reverse['jobs'] = 'array', 'name', True
    app.run_command('savedview save arrays')
    app.save()
    restored = App(store, None, None, app.cfg, 'reviewer')
    restored.views_ref = views
    restored.run_command('facet jobs clear')
    restored.filter = ''
    restored.enter_tab('history')
    restored.run_command('savedview history load arrays')
    assert restored.command_ok and restored.tab == 'jobs'
    assert restored.filter == 'array' and restored.sort['jobs'] == 'name'
    assert restored.reverse['jobs']
    assert restored.table_state['hidden']['jobs'] == ['gpu']
    views.compose(store.snapshot(), restored, 150, 30)
    assert restored.visible_ids == ['101_0', '101_1']  # Descending names: zero, one.
    assert table_ui.matches(restored, 'history', store.finished[0], store.snapshot())


@pytest.mark.parametrize('bad', [{'tab': 'history', 'hidden': ['id']},
                                {'tab': 'history', 'facets': {'bad': 'x'}},
                                {'tab': 'history', 'filter': ['bad']},
                                {'tab': 'history', 'reverse': 'false'},
                                {'tab': 'history', 'days': True}])
def test_damaged_view_is_validated_before_any_navigation_or_mutation(dashboard, bad):
    app, _, _ = dashboard
    app.table_state['views']['damaged'] = bad
    before = (app.tab, app.filter, dict(app.sort), copy.deepcopy(app.table_state))
    app.run_command('savedview load damaged')
    assert not app.command_ok
    assert (app.tab, app.filter, app.sort, app.table_state) == before


def test_array_folding_keeps_actual_task_identity_for_actions(dashboard):
    app, views, store = dashboard
    app.run_command('jobgroups on')
    views.compose(store.snapshot(), app, 150, 30)
    app.cursor['jobs'] = app.visible_ids.index('101_0')
    app.sync_selection()
    app.handle('left')
    _, hits = views.compose(store.snapshot(), app, 150, 30)
    assert '101_1' not in app.visible_ids and app.selected_id == '101_0'
    assert any(value == '101_0' for _, kind, value in hits if kind == 'job')
    assert '101' not in app.visible_ids
    app.handle('right')
    views.compose(store.snapshot(), app, 150, 30)
    assert set(app.visible_ids) == {'101_0', '101_1', '201'}


def test_required_columns_cannot_hide_identity(dashboard):
    app, _, _ = dashboard
    app.run_command('columns hide id,name,st')
    assert not app.command_ok and not app.table_state['hidden']
    table_ui.restore(app, {'hidden': {'jobs': ['id']}, 'facets': {'jobs': {'bad': []}}})
    assert not app.table_state['hidden'] and not app.table_state['facets']


def test_activity_retains_result_paths_and_copy_is_exact(dashboard, monkeypatch):
    app, views, store = dashboard
    path = '/exported/path with spaces/actual.log'
    app.say('Complete file exported', level='success', path=path)
    app.handle('ctrl-a')
    assert app.mode == 'activity'
    notices, _ = app.activity.snapshot()
    assert notices[-1]['path'] == path
    seen = []
    monkeypatch.setattr('raven.clipboard.copy', lambda text, *args, **kwargs: seen.append(text) or 'copied')
    app.handle('y')
    assert seen == [path]
    app.handle('esc')
    assert app.mode == 'main'
    task = app.activity.start('copy actual file', path)
    app.activity.progress(task, 120, 500)
    app.run_command('task cancel')
    assert task['cancel'].is_set()
    app.run_command('activity')
    overlay = views.overlay(store.snapshot(), app, 120, 30)
    assert '120/500 bytes' in '\n'.join(row_text(row) for _, _, row in overlay)


def test_real_full_copy_progress_and_cancellation_removes_partial_exports(tmp_path):
    source = tmp_path / 'source.log'
    source.write_bytes(b'a' * (3 * log_copy.CHUNK_BYTES))
    progress = []
    result = log_copy.copy_full_log(str(source), str(tmp_path / 'state'),
        use_tools=False, use_osc52=False, progress=lambda done, total: progress.append((done, total)),
        cancel=lambda: bool(progress and progress[-1][0] >= log_copy.CHUNK_BYTES))
    assert result['status'] == 'error' and 'cancelled' in result['message']
    assert progress[0] == (0, source.stat().st_size)
    assert progress[-1] == (log_copy.CHUNK_BYTES, source.stat().st_size)
    assert not list((tmp_path / 'state' / 'exports').iterdir())


@pytest.mark.parametrize('character', ['界', 'é', 'λ', '🙂'])
def test_wide_terminal_keys_preserve_printable_unicode(character):
    assert screen.key_name(character, curses) == character


@pytest.mark.parametrize('key, expected', [('\x01', 'ctrl-a'), ('\x02', 'ctrl-b'),
                                          ('\x10', 'ctrl-p'), ('\x17', 'ctrl-w'),
                                          ('\x15', 'ctrl-u'),
                                          ('\n', 'enter'), ('\x7f', 'backspace'),
                                          ('\x1b', 'esc')])
def test_wide_terminal_control_keys_keep_existing_bindings(key, expected):
    assert screen.key_name(key, curses) == expected


def test_f6_decoder_reaches_panel_focus():
    assert screen.key_name(curses.KEY_F6, curses) == 'f6'


def test_every_feature_module_is_required_and_registered():
    assert {module.__name__.removeprefix('raven.') for module in workbench.modules()} == set(workbench.FEATURES)
    assert {'dashboard', 'preflight', 'orchestrate', 'project', 'savedview', 'logview', 'workspaces'} <= set(workbench.command_names())


@pytest.mark.parametrize('tab', ['log', 'research'])
def test_native_page_keys_work_after_leaving_a_focused_details_panel(dashboard, tmp_path, tab):
    app, views, store = dashboard
    app.run_command('focus details')
    assert app.layout_state.focus == 'details' and 'details' in app.layout_state.available
    if tab == 'log':
        path = tmp_path / 'actual.log'
        path.write_bytes(b'first\tcolumn\r\nsecond\r\nthird\n')
        store.details['101_0'] = {'StdOut': str(path)}
        app.open_log('101_0')
        # These keys can arrive before the first native page frame.
        app.handle('home')
        app.handle('v')
        app.handle('down')
        assert app.logs.selection_bytes(app.prepare_log()) == b'first\tcolumn\r\nsecond\r\n'
    else:
        app.enter_tab('research')
        app.research_view, app.research_job_id = 'experiment', '101_1'
        app.handle('up')
        assert app.research_job_id == '101_0'
