import json
from pathlib import Path

import pytest

from raven import launch as L, launch_settings as S


def test_default_loading_and_resolution_write_nothing(tmp_path):
    assert S.load_settings(tmp_path)['launch'] == {}
    assert S.resolve_settings(tmp_path, explicit={'gpu': False}) == ({'gpu': False}, {'gpu': 'command line'})
    assert list(tmp_path.iterdir()) == []


def test_explicit_disabled_values_survive_every_preference_layer(tmp_path):
    user = {'launch': {'gpu': True, 'trace': True, 'features': ['sc']}}
    S.save_settings(tmp_path, {'launch': {'gpu': True, 'trace': False}, 'recipes': {'lab': {'gpu': True, 'features': ['f']}}})
    result, sources = S.resolve_settings(tmp_path, recipe='lab', user_defaults=user, explicit={'gpu': False, 'features': []})
    assert result == {'gpu': False, 'trace': False, 'features': []}
    assert sources == {'gpu': 'command line', 'trace': 'project setting', 'features': 'command line'}


def test_partial_preference_edits_preserve_other_maps(tmp_path):
    S.save_settings(tmp_path, {'launch': {'gpu': True}, 'export_presets': {'Paper figures': {'include': ['application']}}})
    S.update_settings(tmp_path, {'launch': {'trace': True}, 'export_preferences': {'layout': 'metric'}})
    saved = S.load_settings(tmp_path)
    assert saved['launch'] == {'gpu': True, 'trace': True}
    assert 'Paper figures' in saved['export_presets']
    assert saved['export_preferences']['layout'] == 'metric'
    assert (tmp_path / '.raven/automation.json').stat().st_mode & 0o777 == 0o600


def test_complete_save_can_remove_a_preset(tmp_path):
    S.save_settings(tmp_path, {'export_presets': {'old': {}}})
    S.save_settings(tmp_path, {'export_presets': {}})
    assert S.load_settings(tmp_path)['export_presets'] == {}


def test_settings_refuse_symlinked_control_directory(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (tmp_path / '.raven').symlink_to(outside, target_is_directory=True)
    for callback in (lambda: S.load_settings(tmp_path), lambda: S.save_settings(tmp_path, {})):
        with pytest.raises((OSError, ValueError)):
            callback()
    assert list(outside.iterdir()) == []


def test_settings_refuse_symlinked_document_without_changing_target(tmp_path):
    S.save_settings(tmp_path, {})
    outside = tmp_path / 'outside.json'
    outside.write_text('untouched')
    control = tmp_path / '.raven/automation.json'
    control.unlink()
    control.symlink_to(outside)
    for callback in (lambda: S.load_settings(tmp_path), lambda: S.save_settings(tmp_path, {})):
        with pytest.raises((OSError, ValueError)):
            callback()
    assert outside.read_text() == 'untouched'


@pytest.mark.parametrize('settings', [{'launch': {'gpu': 'false'}}, {'launch': {'unknown': True}}, {'recipes': {'invalid name': {}}}, {'default_export_preset': 'missing'}, {'schema': 'other/v1'}, {'launch': {'features': ['unknown']}}])
def test_invalid_save_creates_no_state(tmp_path, settings):
    with pytest.raises(ValueError):
        S.save_settings(tmp_path, settings)
    assert not (tmp_path / '.raven').exists()


def test_completion_preset_captured_and_can_be_explicitly_disabled(tmp_path):
    script = tmp_path / 'work.sh'
    script.write_text('#!/bin/sh\ntrue\n')
    S.save_settings(tmp_path, {'launch': {'auto_export': {'include': ['summary']}}, 'export_presets': {'Paper figures': {'include': ['application'], 'metrics': ['loss'], 'destination': {'mode': 'project', 'path': '', 'host': '', 'user': 'alice'}}}, 'default_export_preset': 'Paper figures'})
    result = L.resolve_plan([str(script), '--export-on-finish'], environ={})
    assert result['auto_export']['metrics'] == ['loss']
    assert 'user' not in result['auto_export']['destination']
    saved = S.load_settings(tmp_path)
    saved['export_presets']['Paper figures']['metrics'] = ['accuracy']
    S.save_settings(tmp_path, saved)
    assert result['auto_export']['metrics'] == ['loss']
    assert L.resolve_plan([str(script), '--no-export-on-finish'], environ={})['auto_export'] is False


def test_replace_one_settings_section_preserves_concurrent_other_sections(tmp_path):
    S.save_settings(tmp_path, {'launch': {'gpu': True, 'trace': True}, 'export_presets': {'Figures': {}}})
    S.update_settings(tmp_path, {'launch': {'gpu': False}}, replace_sections=('launch',))
    saved = S.load_settings(tmp_path)
    assert saved['launch'] == {'gpu': False}
    assert saved['export_presets'] == {'Figures': {}}


@pytest.mark.parametrize('launch', [{'telemetry': {'interval_seconds': 0}}, {'telemetry': {'gpu_provider': 'imaginary'}}, {'telemetry': {'max_bytes_per_stream': 1e12}}, {'scientific_context': {'repeat': 0}}, {'scientific_context': {'workers': 3.5}}, {'scientific_context': {'problem_size': float('nan')}}])
def test_invalid_collector_or_scientific_declarations_fail_before_state(tmp_path, launch):
    with pytest.raises(ValueError):
        S.save_settings(tmp_path, {'launch': launch})
    assert not (tmp_path / '.raven').exists()


def test_published_schemas_accept_native_resolved_documents(tmp_path):
    jsonschema = pytest.importorskip('jsonschema')
    from referencing import Registry, Resource
    schema_root = Path(__file__).parents[1] / 'docs' / 'schemas'
    documents = {name: json.loads((schema_root / name).read_text()) for name in ('launch-plan.v1.schema.json', 'automation-settings.v1.schema.json')}
    registry = Registry().with_resources([(name, Resource.from_contents(doc)) for name, doc in documents.items()])
    for document in documents.values():
        jsonschema.Draft202012Validator.check_schema(document)
    script = tmp_path / 'work.sh'
    script.write_text('#!/bin/sh\ntrue\n')
    settings = S.save_settings(tmp_path, {'launch': {'features': ['sc'], 'telemetry': {'interval_seconds': 2}}, 'export_presets': {'Paper figures': {'include': ['all']}}})
    plan = L.resolve_plan([str(script), '--check'], environ={})
    jsonschema.Draft202012Validator(documents['automation-settings.v1.schema.json'], registry=registry).validate(settings)
    jsonschema.Draft202012Validator(documents['launch-plan.v1.schema.json'], registry=registry).validate(plan)


@pytest.mark.parametrize('destination', [{'mode': 'absolute', 'path': '/scratch/figures', 'host': 'definitely-other-host.example'}, {'mode': 'absolute', 'path': '/scratch/figures', 'host': 'local', 'user': 'definitely-another-user'}])
def test_completion_preferences_do_not_reuse_another_host_or_user(tmp_path, destination):
    script = tmp_path / 'run.sh'
    script.write_text('#!/bin/sh\ntrue\n')
    S.save_settings(tmp_path, {'export_presets': {'Figures': {'destination': destination}}, 'default_export_preset': 'Figures'})
    with pytest.raises(ValueError, match='belongs to another'):
        L.resolve_plan([str(script), '--export-on-finish', '--check'], environ={})
    saved = S.load_settings(tmp_path)
    saved['launch']['auto_export'] = saved['export_presets']['Figures']
    S.save_settings(tmp_path, saved)
    with pytest.raises(ValueError, match='belongs to another'):
        L.resolve_plan([str(script), '--check'], environ={})


def test_future_absolute_export_scope_survives_stripped_request_metadata(tmp_path):
    script = tmp_path / 'work.sh'
    script.write_text('#!/bin/sh\ntrue\n')
    S.save_settings(tmp_path, {'launch': {'auto_export': {'destination': {'mode': 'absolute', 'path': '/scratch/figures', 'host': 'local'}}, 'auto_export_scope': {'host': 'local', 'user': 'another-project-user'}}})
    with pytest.raises(ValueError, match='another user'):
        L.resolve_plan([str(script), '--check'], environ={})
    assert L.resolve_plan([str(script), '--no-export-on-finish', '--check'], environ={})['auto_export'] is False
    assert L.resolve_plan([str(script), '--export-on-finish', '--check'], environ={})['auto_export']['destination']['mode'] == 'project'


@pytest.mark.parametrize('options', [
    {'range_policy': 'chart'}, {'range_policy': 'explicit'},
    {'start': 1700000000}, {'end': 1700000010},
    {'range_policy': 'full', 'start': 1700000000},
])
@pytest.mark.parametrize('origin', ['preset', 'project', 'recipe'])
def test_future_completion_rejects_previous_attempt_time_range(tmp_path, options, origin):
    script = tmp_path / 'work.sh'
    script.write_text('#!/bin/sh\ntrue\n')
    request = dict(include=['application'], **options)
    settings = {'export_presets': {'Old chart': request}}
    arguments = ['--export-on-finish', 'Old chart']
    if origin == 'project':
        settings['launch'] = {'auto_export': request}
        arguments = []
    elif origin == 'recipe':
        settings['recipes'] = {'lab': {'auto_export': request}}
        arguments = ['--recipe', 'lab']
    S.save_settings(tmp_path, settings)
    before = {str(path): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    with pytest.raises(ValueError, match='full recorded run'):
        L.resolve_plan([str(script), *arguments, '--check'], environ={})
    assert {str(path): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()} == before
    retained_recipe = arguments if origin == 'recipe' else []
    assert L.resolve_plan([str(script), *retained_recipe, '--no-export-on-finish', '--check'], environ={})['auto_export'] is False
