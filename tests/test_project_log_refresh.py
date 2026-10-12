"""Report refreshes invalidate project sources without revoking native logs."""
import copy
from types import SimpleNamespace

import pytest

from raven import log_workbench, project_ui
from raven.config import Config
from raven.controller import App
from raven.model import Store


class PublishedHub:
    def __init__(self):
        self.settings = {}
        self.passport = self.passport_diff = None

    def configure(self, **settings):
        self.settings.update(settings)


@pytest.fixture
def bound():
    app = App(Store(persist=False), None, None, Config(), 'test', interactive=False)
    app.research = PublishedHub()
    binding = {'project_root': '/scratch/project', 'run_root': '/scratch/project/runs/fit',
               'run_id': 'fit', 'job_id': '7', 'attempt': 1, 'state': 'RUNNING',
               'metrics_file': '/scratch/project/runs/fit/metrics.jsonl', 'contract': '',
               'passport': '', 'log_manifest': '',
               'stdout': '/scratch/project/runs/fit/logs/stdout.log',
               'stderr': '/scratch/project/runs/fit/logs/stderr.log'}
    logs = [{'id': 'project.' + role, 'path': binding[role], 'label': role, 'role': role,
             'source': 'project', 'group': 'Run streams', 'run_id': 'fit', 'job_id': '7'}
            for role in ('stdout', 'stderr')]
    value = {'binding': binding, 'logs': logs, 'warnings': []}
    project_ui._apply_run(app, copy.deepcopy(value), automatic=True)
    native = [{'id': 'scheduler.' + role, 'path': '/scratch/scheduler.' + ('out' if role == 'stdout' else 'err'),
               'label': role, 'source': 'scheduler', 'group': 'Scheduler'}
              for role in ('stdout', 'stderr')]
    app.logs.entry = native[0]
    app.logs.entries = native + copy.deepcopy(logs)
    app.logs.path, app.logs.top, app.logs.cursor = native[0]['path'], 11, 12
    app.logs.selection_anchor, app.logs.selection_end = 2, 4
    app.logs.selection_path = native[0]['path']
    app.log_job = '7'
    return SimpleNamespace(app=app, value=value, native=native)


@pytest.mark.parametrize('source', ['scheduler', 'discovered', 'directory'])
def test_same_attempt_refresh_preserves_independent_catalog_source_and_position(bound, source, monkeypatch):
    app = bound.app
    app.logs.entry = dict(app.logs.entry, source=source)
    entry, entries = app.logs.entry, app.logs.entries
    before = app.logs.path, app.logs.top, app.logs.cursor, app.logs.selection_anchor, app.logs.selection_end
    monkeypatch.setattr(app.store, 'snapshot', lambda: pytest.fail('Report refresh copied scheduler snapshots'))
    value = copy.deepcopy(bound.value)
    value['binding']['state'] = 'COMPLETED'
    project_ui._apply_run(app, value, automatic=True, refresh=True)
    assert app.logs.entry is entry and app.logs.entries is entries
    assert (app.logs.path, app.logs.top, app.logs.cursor, app.logs.selection_anchor, app.logs.selection_end) == before
    assert [entry['path'] for entry in log_workbench._split_entries(app)] == [row['path'] for row in bound.native]


@pytest.mark.parametrize('source', ['project', 'manifest', None])
def test_removed_project_declaration_resets_its_selected_log(bound, source):
    app = bound.app
    app.logs.entry = copy.deepcopy(bound.value['logs'][0])
    if source is None:
        app.logs.entry.pop('source')
    else:
        app.logs.entry['source'] = source
    app.logs.path = app.logs.entry['path']
    value = copy.deepcopy(bound.value)
    value['logs'] = value['logs'][1:]
    value['binding']['stdout'] = ''
    project_ui._apply_run(app, value, automatic=True, refresh=True)
    assert app.logs.entry is None and app.logs.entries == []
    assert app.logs.path == '' and app.logs.top is None
    assert app.logs.selection_anchor is None


def test_discovered_provenance_preserves_a_file_even_if_its_project_declaration_is_removed(bound):
    app = bound.app
    app.logs.entry = {'id': 'file.native', 'path': bound.value['binding']['stdout'],
                      'label': 'Observed job output', 'source': 'discovered', 'group': 'Discovered'}
    entry = app.logs.entry
    value = copy.deepcopy(bound.value)
    value['logs'] = value['logs'][1:]
    value['binding']['stdout'] = ''
    project_ui._apply_run(app, value, automatic=True, refresh=True)
    assert app.logs.entry is entry
    assert app.logs.entries


@pytest.mark.parametrize('field,new_value', [('project_root', '/scratch/other'), ('run_id', 'other'),
                                           ('job_id', '8'), ('attempt', 2)])
def test_changed_run_job_or_attempt_resets_even_an_independent_native_source(bound, field, new_value):
    value = copy.deepcopy(bound.value)
    value['binding'][field] = new_value
    project_ui._apply_run(bound.app, value, automatic=True, refresh=True)
    assert bound.app.logs.entry is None and bound.app.logs.entries == []
    assert bound.app.logs.path == ''
    assert bound.app.log_job == '7'  # Refresh does not silently open another job.


def test_legacy_bindings_with_no_attempt_compare_consistently(bound):
    bound.app.project_state['binding'].pop('attempt')
    value = copy.deepcopy(bound.value)
    value['binding'].pop('attempt')
    entry, entries = bound.app.logs.entry, bound.app.logs.entries
    project_ui._apply_run(bound.app, value, automatic=True, refresh=True)
    assert bound.app.logs.entry is entry and bound.app.logs.entries is entries


def test_bound_empty_catalog_uses_published_project_pair_without_io(bound, monkeypatch):
    app = bound.app
    app.logs.entry, app.logs.entries, app.logs.path = None, [], ''
    monkeypatch.setattr(app.store, 'snapshot', lambda: pytest.fail('Bound paired view copied a store snapshot'))
    monkeypatch.setattr(app, 'log_target', lambda *_args: pytest.fail('Bound paired view queried the scheduler target'))
    entries = log_workbench._entries(app)
    assert [entry['path'] for entry in entries] == [entry['path'] for entry in bound.value['logs']]
    assert len(log_workbench._split_entries(app)) == 2
    assert app.logs.entries == []  # The fallback does not impersonate a native catalog publication.


def test_native_catalog_remains_authoritative_when_it_is_already_published(bound):
    entries = log_workbench._entries(bound.app)
    assert entries == bound.app.logs.entries
    assert [entry['path'] for entry in log_workbench._split_entries(bound.app)] == [row['path'] for row in bound.native]
