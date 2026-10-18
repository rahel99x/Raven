"""Recording preferences preserve unrelated settings and run off the UI path."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from raven import raven_config as C, setup_ui as U
from raven.config import Config
from raven.controller import App
from raven.model import Store, Job
from raven.series_settings import save_format


class Background:
    demo = False
    settings = {}
    pending = None
    def start_task(self, read, complete):
        from raven.raven_execution import background_allowed
        assert background_allowed(SimpleNamespace(managed=True), read)
        if self.pending: return False
        self.pending = read, complete
        return True
    def finish(self):
        read, complete = self.pending; self.pending = None
        try: value = read()
        except Exception as exc: value = exc
        complete(value)


def test_format_save_is_scoped_and_keeps_other_edits(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'schema': C.SCHEMA, 'allocation': {'account': 'lab'}, 'profiles': {'project': {'allocation': {'partition': 'cpu'}}}}))
    result = save_format({'path': str(path), 'profile': 'project', 'expected': 'jsonl', 'format': 'blocks-v1'})
    value = json.loads(path.read_text())
    assert value['allocation']['account'] == 'lab'
    assert value['profiles']['project'] == {'allocation': {'partition': 'cpu'}, 'storage': {'series_format': 'blocks-v1'}}
    assert result['format'] == 'blocks-v1'
    with pytest.raises(ValueError, match='changed'):
        save_format({'path': str(path), 'profile': 'project', 'expected': 'jsonl', 'format': 'jsonl'})


def test_setup_preview_and_conversion_are_background_scoped_actions(tmp_path):
    path = tmp_path / 'config.json'; path.write_text(json.dumps({'schema': C.SCHEMA}))
    resolved = C.resolve(str(path), env={'HOME': str(tmp_path)}, system_policy_paths=[])
    cfg = Config(C.to_raven_config(resolved))
    store = Store(str(tmp_path / 'state'))
    record = Job('7', 'train', 'cpu', 'RUNNING', submit='2026-10-17T10:00:00', start='2026-10-17T10:00:01')
    store.apply_jobs([record]); details = dict(JobId='7', SubmitTime=record.submit, StartTime=record.start, Restarts='0')
    store.details['7'] = details; store.bind_series_scheduler('7', details)
    store.record('7', {'t': 1., 'cpu': .5})
    original = Path(store._series_path('7'))
    app = App(store, None, None, cfg, 'student')
    app.raven_resolved = resolved; app.research = Background(); app.selected_id = '7'
    assert U.run_command(app, ['setup', 'storage'])
    assert U.run_command(app, ['setup', 'storage-preview'])
    assert app.research.pending and not original.with_suffix('.rvb').exists()
    app.research.finish()
    assert app.setup_ui_state['storage_result']['status'] == 'preview'
    assert U.run_command(app, ['setup', 'storage-convert'])
    assert not original.with_suffix('.rvb').exists()
    app.research.finish()
    assert original.exists() and original.with_suffix('.rvb').exists()
    assert 'source preserved' in app.setup_ui_state['message']
    assert U.run_command(app, ['setup', 'storage-format', 'blocks-v1'])
    assert store.series_storage == 'jsonl'
    app.research.finish()
    assert store.series_storage == 'blocks-v1'
    assert C.resolve(str(path), env={'HOME': str(tmp_path)}, system_policy_paths=[]).data['storage']['series_format'] == 'blocks-v1'
    assert U.run_command(app, ['setup', 'storage-retain-preview'])
    app.research.finish()
    assert app.setup_ui_state['retention_result']['status'] == 'preview'
    assert U.run_command(app, ['setup', 'storage-retain'])
    app.research.finish()
    assert app.setup_ui_state['retention_result']['status'] == 'published'
    assert U.run_command(app, ['setup', 'storage-tier-query'])
    app.research.finish()
    assert any('RAW t=1.0' in text for text, _ in U._document(app))
    store.close()
