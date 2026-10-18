"""Trace settings preserve CLI precedence, saved preferences and read-only previews."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven import launch, launch_settings, trace_ui, workbench


def script(tmp_path):
    path = tmp_path / 'job.sh'
    path.write_text('#!/bin/bash\npython3 solve.py\n')
    (tmp_path / 'solve.py').write_text('import raven\nraise RuntimeError("Readiness must not execute application code")\n')
    return path


def test_trace_plan_defaults_and_api_readiness_are_read_only(tmp_path):
    path = script(tmp_path)
    before = {str(p): p.read_bytes() for p in tmp_path.iterdir()}
    plan = launch.resolve_plan([str(path), '--trace', '--check'], cwd=tmp_path, environ={})
    assert {name: plan['settings'][name] for name in launch_settings.TRACE_DEFAULTS} == launch_settings.TRACE_DEFAULTS
    assert any('@raven client detected in solve.py' in row['reason'] for row in plan['readiness'])
    assert any('no script hook is required' in row['reason'] for row in plan['readiness'])
    assert {str(p): p.read_bytes() for p in tmp_path.iterdir()} == before


def test_trace_cli_preferences_override_saved_recipe_without_rewriting_it(tmp_path):
    path = script(tmp_path)
    saved = {'trace': True, 'trace_scope': 'all', 'trace_helpers': True,
             'trace_process_bytes': 8192, 'trace_run_bytes': 32768, 'trace_processes': 3}
    launch_settings.save_settings(tmp_path, {'launch': saved, 'recipes': {'small': {'trace_processes': 2}}})
    plan = launch.resolve_plan([str(path), '--recipe', 'small', '--trace-scope', 'project', '--no-trace-helpers',
                               '--trace-process-bytes', '16384', '--trace-run-bytes', '65536', '--check'], cwd=tmp_path, environ={})
    assert plan['settings']['trace_scope'] == 'project'
    assert plan['settings']['trace_helpers'] is False
    assert plan['settings']['trace_processes'] == 2
    assert plan['settings']['trace_process_bytes'] == 16384
    assert plan['settings']['trace_run_bytes'] == 65536
    assert plan['setting_sources']['trace_helpers'] == 'command line'
    assert launch_settings.load_settings(tmp_path)['launch'] == saved
    assert launch.resolve_plan([str(path), '--trace-helpers'], cwd=tmp_path, environ={})['settings']['trace_helpers'] is True


def test_captured_settings_and_legacy_namespaces_have_default_trace_values(tmp_path):
    path = script(tmp_path)
    args = launch.parse([str(path), '--check'])
    for name in launch_settings.TRACE_DEFAULTS:
        delattr(args, name)
    plan = launch.resolve_plan(args, cwd=tmp_path, environ={}, captured_settings={'trace': True, 'trace_helpers': True})
    assert plan['settings']['trace_helpers'] is True
    assert plan['settings']['trace_scope'] == 'project'
    assert plan['setting_sources']['trace_helpers'] == 'retained run settings'


@pytest.mark.parametrize('field,minimum,maximum', [('trace_process_bytes',4096,1<<30), ('trace_run_bytes',4096,4<<30), ('trace_processes',1,512)])
@pytest.mark.parametrize('invalid', ['small', 'large', 'bool', 'float'])
def test_invalid_trace_budgets_are_rejected_before_state_creation(tmp_path, field, minimum, maximum, invalid):
    value = {'small':minimum-1, 'large':maximum+1, 'bool':True, 'float':float(minimum)}[invalid]
    with pytest.raises(ValueError, match='trace'):
        launch_settings.save_settings(tmp_path, {'launch': {field:value}})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('field,value', [('trace_scope','native'),('trace_scope',False),('trace_helpers',1),('trace_helpers','false')])
def test_invalid_trace_semantics_are_rejected(tmp_path, field, value):
    with pytest.raises(ValueError, match='trace'):
        launch_settings.save_settings(tmp_path, {'launch': {field:value}})
    assert list(tmp_path.iterdir()) == []


def test_trace_budgets_validate_cli_and_captured_override(tmp_path):
    path = script(tmp_path)
    for captured in (None, {'trace':True}):
        with pytest.raises(ValueError, match='trace_processes'):
            launch.resolve_plan([str(path),'--trace-processes','513'], cwd=tmp_path, environ={}, captured_settings=captured)
    assert not (tmp_path / '.raven').exists()


def test_application_trace_named_arguments_after_separator_are_preserved(tmp_path):
    path = script(tmp_path)
    plan = launch.resolve_plan([str(path),'--','--trace-scope','all'], cwd=tmp_path, environ={})
    assert plan['script_args'] == ['--trace-scope','all']
    assert plan['settings']['trace_scope'] == 'project'


def test_explicit_trace_schema_validates_preferences_and_plan(tmp_path):
    jsonschema = pytest.importorskip('jsonschema')
    from referencing import Registry, Resource
    directory = Path(__file__).parents[1] / 'docs/schemas'
    documents = {name:json.loads((directory / name).read_text()) for name in ('launch-plan.v1.schema.json','automation-settings.v1.schema.json')}
    registry = Registry().with_resources([(name,Resource.from_contents(value)) for name,value in documents.items()])
    path = script(tmp_path)
    settings = launch_settings.save_settings(tmp_path, {'launch':dict(launch_settings.TRACE_DEFAULTS, trace=True, trace_helpers=True)})
    plan = launch.resolve_plan([str(path),'--trace-scope','all'], cwd=tmp_path, environ={})
    jsonschema.Draft202012Validator(documents['automation-settings.v1.schema.json'],registry=registry).validate(settings)
    jsonschema.Draft202012Validator(documents['launch-plan.v1.schema.json'],registry=registry).validate(plan)


def test_trace_helper_choice_persists_through_actual_workbench_extension_points():
    from raven.config import Config
    from raven.controller import App
    from raven.model import Store
    app = App(Store(persist=False),None,None,Config({}),'researcher')
    trace_ui.run_command(app,['trace','helpers'])
    saved = workbench.save(app)
    assert saved['trace_ui'] == {'show_helpers':True}
    restored = App(Store(persist=False),None,None,Config({}),'researcher')
    workbench.restore(restored,saved)
    assert trace_ui.initialize(restored)['show_helpers'] is True
    trace_ui.run_command(restored,['trace','helpers'])
    assert workbench.save(restored)['trace_ui'] == {'show_helpers':False}
