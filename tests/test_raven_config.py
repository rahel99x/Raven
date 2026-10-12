import copy
import json
import os
from pathlib import Path

import pytest

from tower import raven_config as C


def environment(tmp_path):
    return {'HOME':str(tmp_path),'XDG_CONFIG_HOME':str(tmp_path/'config'),'XDG_STATE_HOME':str(tmp_path/'state')}


def write(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data));return str(path)


def resolve(tmp_path,**kwargs):
    return C.resolve(env=environment(tmp_path),system_policy_paths=[],**kwargs)


def test_light_default_never_inferrs_desktop_from_missing_tools(tmp_path,monkeypatch):
    monkeypatch.setattr(C.shutil,'which',lambda _:None)
    result=resolve(tmp_path)
    assert result.data['site']['kind']=='slurm'
    assert result.data['execution']['startup']=='light'
    assert result.data['execution']['heavy_requires_allocation']
    assert result.data['allocation']['auto_request_on_light_start'] is False
    assert C.doctor(result)['tools']['squeue']['status']=='unsupported'
    assert not (tmp_path/'state').exists()


def test_desktop_requires_explicit_profile(tmp_path):
    result=resolve(tmp_path,site='desktop',launcher='heavy')
    assert result.data['execution']['desktop_execution']=='explicit'
    assert result.data['connection']['transport']=='none'
    assert not result.data['collection']['slurm_enabled']
    assert result.data['execution']['startup']=='heavy'


def test_light_overrides_saved_heavy_startup(tmp_path):
    path=write(tmp_path/'config.json',{'schema':C.SCHEMA,'execution':{'startup':'heavy'}})
    value=resolve(tmp_path,config_path=path)
    assert value.data['execution']['startup']=='light'
    assert 'ravenl' in value.origins['execution.startup']


@pytest.mark.parametrize('overlay',[
    {'typo':1},{'schema':'raven.config/v9'},{'collection':{'queue_seconds':'60s'}},
    {'collection':{'max_inflight':True}},{'collection':{'max_inflight':1.5}},
    {'collection':{'queue_seconds':float('nan')}},{'collection':{'queue_seconds':10**500}},
    {'allocation':{'auto_request_on_light_start':True}},{'allocation':{'auto_renew':True}},
    {'execution':{'allow_login_heavy':True}},{'collection':{'weather_probes':True}},
    {'site':{'kind':'guess'}},{'allocation':{'adapter':'run-arbitrary-shell'}},
    {'resources':{'heavy':{'max_workers':300}}},{'profiles':{'nested':{'profiles':{}}}},
    {'execution':{'capabilities':['unknown-plugin']}},{'connection':{'ssh_options':['bad\x00argument']}},
])
def test_strict_schema_rejects_invalid_input(tmp_path,overlay):
    path=write(tmp_path/'config.json',{'schema':C.SCHEMA,**overlay})
    with pytest.raises(ValueError):resolve(tmp_path,config_path=path)


def test_file_bytes_budget_is_not_request_count(tmp_path):
    result=resolve(tmp_path)
    assert result.data['collection']['file_bytes_per_minute']==16<<20
    C.validate(result.data)


def test_step_launch_defaults_match_collection_admission(tmp_path):
    from tower.raven_collection import DEFAULTS
    collection = resolve(tmp_path).data['collection']
    assert collection['step_launch_interval_seconds'] == DEFAULTS['step_launch_interval_seconds'] == 10
    assert collection['step_launches_per_minute'] == DEFAULTS['step_launches_per_minute'] == 6


def test_step_launch_interval_and_quota_follow_mandatory_policy(tmp_path):
    policy = write(tmp_path/'policy.json', {'schema': C.POLICY_SCHEMA, 'restrictions': {
        'floors': {'collection.step_launch_interval_seconds': 30},
        'ceilings': {'collection.step_launches_per_minute': 1}}})
    result = C.resolve(env=environment(tmp_path), system_policy_paths=[policy],
                       cli_overrides={'collection': {'step_launch_interval_seconds': .5,
                                                    'step_launches_per_minute': 6}})
    assert result.data['collection']['step_launch_interval_seconds'] == 30
    assert result.data['collection']['step_launches_per_minute'] == 1
    assert result.origins['collection.step_launch_interval_seconds'] == 'site restriction'


def test_existing_step_quota_is_preserved_and_missing_interval_uses_default(tmp_path):
    path = write(tmp_path/'config.json', {'schema': C.SCHEMA,
                                        'collection': {'step_launches_per_minute': 1}})
    original = Path(path).read_bytes()
    result = resolve(tmp_path, config_path=path)
    assert result.data['collection']['step_launches_per_minute'] == 1
    assert result.data['collection']['step_launch_interval_seconds'] == 10
    assert Path(path).read_bytes() == original


@pytest.mark.parametrize('interval', [0, -.1, .01, True, '10', 31536001])
def test_step_launch_interval_rejects_invalid_units_and_bounds(interval):
    import jsonschema
    document = {'schema': C.SCHEMA, 'collection': {'step_launch_interval_seconds': interval}}
    with pytest.raises(ValueError):
        C.validate(document, partial=True)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(C.schema_document()).validate(document)


def test_step_launch_interval_accepts_fractional_seconds():
    import jsonschema
    document = {'schema': C.SCHEMA, 'collection': {'step_launch_interval_seconds': .5}}
    C.validate(document, partial=True)
    jsonschema.Draft202012Validator(C.schema_document()).validate(document)


def test_profile_cli_site_and_user_values_precedence(tmp_path):
    path=write(tmp_path/'config.json',{'schema':C.SCHEMA,'profile':'a','collection':{'queue_seconds':100},
        'profiles':{'a':{'allocation':{'account':'old'}},'b':{'allocation':{'account':'new'},'collection':{'queue_seconds':120}}}})
    result=resolve(tmp_path,config_path=path,site='b',cli_overrides={'collection':{'queue_seconds':180}})
    assert result.data['allocation']['account']=='new'
    assert result.data['collection']['queue_seconds']==180
    assert result.origins['collection.queue_seconds']=='CLI'


def test_builtin_desktop_clears_cluster_transport(tmp_path):
    path=write(tmp_path/'config.json',{'schema':C.SCHEMA,'profile':'cluster','connection':{'transport':'ssh','host':'hpc'}})
    result=resolve(tmp_path,config_path=path,site='desktop')
    assert result.data['connection']['host']==''
    assert result.data['connection']['transport']=='none'


def test_site_policy_floors_ceilings_and_capability_intersection(tmp_path):
    first=write(tmp_path/'policy1.json',{'schema':C.POLICY_SCHEMA,'restrictions':{
        'floors':{'collection.queue_seconds':90},'ceilings':{'resources.heavy.max_workers':1},
        'allowed_capabilities':['view','heavy-analysis']}})
    second=write(tmp_path/'policy2.json',{'schema':C.POLICY_SCHEMA,'restrictions':{
        'floors':{'collection.queue_seconds':120},'allowed_capabilities':['view']}})
    result=C.resolve(env=environment(tmp_path),system_policy_paths=[first],site_policy_path=second,
                     cli_overrides={'collection':{'queue_seconds':.5},'resources':{'heavy':{'max_workers':16}}})
    assert result.data['collection']['queue_seconds']==120
    assert result.data['resources']['heavy']['max_workers']==1
    assert result.data['execution']['capabilities']==['view']
    assert result.origins['collection.queue_seconds']=='site restriction'
    with pytest.raises(ValueError,match='disables new Heavy'):
        C.resolve(env=environment(tmp_path),system_policy_paths=[first],site_policy_path=second,launcher='heavy')


@pytest.mark.parametrize('restriction',[
    {'allow_desktop':False},{'allowed_site_kinds':['slurm']},{'require_allocation':True},
    {'allowed_targets':['slurm']},{'allowed_capabilities':[]},
])
def test_user_desktop_cannot_override_mandatory_site_restrictions(tmp_path,restriction):
    path=write(tmp_path/'policy.json',{'schema':C.POLICY_SCHEMA,'restrictions':restriction})
    with pytest.raises(ValueError):
        C.resolve(site='desktop',launcher='heavy',env=environment(tmp_path),system_policy_paths=[path])


def test_missing_account_fails_before_admission(tmp_path):
    path=write(tmp_path/'policy.json',{'schema':C.POLICY_SCHEMA,'restrictions':{'require_account':True}})
    C.resolve(env=environment(tmp_path),system_policy_paths=[path],launcher='light')
    with pytest.raises(ValueError,match='requires an account'):
        C.resolve(env=environment(tmp_path),system_policy_paths=[path],launcher='heavy')


def test_new_config_precedes_legacy_without_merge_or_writes(tmp_path):
    env=environment(tmp_path)
    old=Path(write(Path(env['XDG_CONFIG_HOME'])/'tower/config.json',{'theme':'mono','host':'old-host','intervals':{'jobs':.5}}))
    old_bytes=old.read_bytes()
    new=write(Path(env['XDG_CONFIG_HOME'])/'raven/config.json',{'schema':C.SCHEMA,'ui':{'theme':'dark'}})
    result=C.resolve(env=env,system_policy_paths=[])
    assert result.source==new and not result.legacy_source
    assert result.data['ui']['theme']=='dark' and result.data['connection']['host']==''
    assert old.read_bytes()==old_bytes and not Path(env['XDG_STATE_HOME']).exists()


def test_legacy_view_maps_semantics_and_retains_profile_names(tmp_path):
    env=environment(tmp_path)
    old=write(Path(env['XDG_CONFIG_HOME'])/'tower/config.json',{'theme':'mono','worker_mode':'multi',
        'intervals':{'jobs':.5,'finished':600},'profiles':{'lab':{'account':'my-lab'}}})
    result=C.resolve(site='lab',env=env,system_policy_paths=[])
    assert result.legacy_source==old
    assert result.data['collection']['queue_seconds']==60 and result.data['collection']['history_seconds']==600
    assert result.data['ui']['worker_mode']=='multi' and result.data['execution']['startup']=='light'
    assert result.data['allocation']['account']=='my-lab'
    assert result.explain()['legacy_view']


def test_new_environment_names_win_and_warnings_do_not_leak_values(tmp_path):
    new=write(tmp_path/'new.json',{'schema':C.SCHEMA,'ui':{'theme':'dark'}})
    old=write(tmp_path/'old-secret.json',{'theme':'mono'})
    env={**environment(tmp_path),'RAVEN_CONFIG':new,'TOWER_CONFIG':old,
         'RAVEN_STATE_DIR':str(tmp_path/'new-state'),'TOWER_STATE_DIR':'secret-state-value'}
    result=C.resolve(env=env,system_policy_paths=[])
    assert result.data['ui']['theme']=='dark'
    assert result.data['storage']['state_dir']==str(tmp_path/'new-state')
    assert 'old-secret' not in '\n'.join(result.warnings) and 'secret-state-value' not in '\n'.join(result.warnings)


def test_explain_redacts_paths_and_credential_reference(tmp_path):
    path=write(tmp_path/'config.json',{'schema':C.SCHEMA,'connection':{'credential_reference':'private-agent-reference'}})
    result=resolve(tmp_path,config_path=path)
    value=result.explain()
    assert value['effective']['storage']['state_dir']=='<configured path>'
    assert 'private-agent-reference' not in json.dumps(value)
    assert result.explain(show_paths=True)['effective']['storage']['state_dir'].startswith(str(tmp_path))


def test_reload_is_atomic_and_does_not_mutate_original(tmp_path):
    path=Path(write(tmp_path/'config.json',{'schema':C.SCHEMA,'ui':{'theme':'mono'}}))
    original=resolve(tmp_path,config_path=str(path));before=copy.deepcopy(original.data)
    path.write_text('{"schema":"raven.config/v1","unknown":true}')
    with pytest.raises(ValueError):C.reload_config(original,env=environment(tmp_path),system_policy_paths=[])
    assert original.data==before
    path.write_text('{"schema":"raven.config/v1","ui":{"theme":"dark"}}')
    newer=C.reload_config(original,env=environment(tmp_path),system_policy_paths=[])
    assert newer.digest!=original.digest and original.data==before


def test_worker_independently_rechecks_mandatory_policy(tmp_path):
    path=write(tmp_path/'policy.json',{'schema':C.POLICY_SCHEMA,'restrictions':{'allow_desktop':False}})
    with pytest.raises(ValueError,match='forbid desktop'):
        C.worker_policy_check({'site_kind':'desktop','desktop_explicit':True,'resources':{}},system_policy_paths=[path])
    with pytest.raises(ValueError,match='explicit'):
        C.worker_policy_check({'site_kind':'desktop','resources':{}},system_policy_paths=[])


def test_worker_refuses_resource_policy_clamp(tmp_path):
    path=write(tmp_path/'policy.json',{'schema':C.POLICY_SCHEMA,'restrictions':{'ceilings':{'resources.heavy.memory_mib':1024}}})
    with pytest.raises(ValueError,match='exceed mandatory'):
        C.worker_policy_check({'site_kind':'desktop','desktop_explicit':True,'resources':{'memory_mib':4096}},system_policy_paths=[path])


def test_legacy_bridge_embeds_managed_and_conservative_defaults(tmp_path):
    result=resolve(tmp_path)
    values=C.to_tower_config(result)
    assert values['raven']['managed'] and values['raven']['schema']==C.SCHEMA
    assert not values['gpu_sampling'] and not values['weather'] and not values['plugins']
    assert values['intervals']['jobs']==60 and values['intervals']['finished']==300
    assert values['terminal']['max_sessions']==2
    assert 'managed' not in result.data


def test_schema_and_json_examples_are_synchronized():
    root=Path(__file__).resolve().parents[1]
    assert json.loads((root/'docs/schemas/raven-config.schema.json').read_text())==C.schema_document()
    import jsonschema
    for name in ('desktop','slurm'):
        data=json.loads((root/f'docs/examples/raven/{name}.json').read_text())
        C.validate(data)
        jsonschema.Draft202012Validator(C.schema_document()).validate(data)


def test_toml_examples_or_clear_old_python_diagnostic():
    import sys
    root=Path(__file__).resolve().parents[1]
    for name in ('desktop','slurm'):
        path=root/f'docs/examples/raven/{name}.toml'
        if sys.version_info < (3,11):
            with pytest.raises(ValueError,match='JSON'):C.read_file(path)
        else:
            data=C.merge(C.DEFAULTS,C.read_file(path));C.validate(data)


def test_fifo_symlink_duplicate_keys_and_oversized_config_rejected(tmp_path):
    fifo=tmp_path/'fifo';os.mkfifo(fifo)
    with pytest.raises(ValueError):C.read_file(fifo)
    path=tmp_path/'config.json';path.write_text('{"schema":"raven.config/v1","schema":"other"}')
    with pytest.raises(ValueError,match='Duplicate'):C.read_file(path)
    link=tmp_path/'link';link.symlink_to(path)
    with pytest.raises(OSError):C.read_file(link)
    path.write_bytes(b' '*(C.MAX_CONFIG+1))
    with pytest.raises(ValueError):C.read_file(path)


@pytest.mark.parametrize('owner',['process','site-service'])
def test_unqualified_collection_owners_rejected(tmp_path,owner):
    with pytest.raises(ValueError,match='no qualified adapter'):
        resolve(tmp_path,cli_overrides={'collection':{'owner':owner}})


def test_legacy_desktop_profile_is_slurm_but_site_desktop_is_explicit(tmp_path):
    path=write(tmp_path/'legacy.json',{'profiles':{'desktop':{'account':'workstation'}}})
    old=resolve(tmp_path,config_path=path,legacy_profile='desktop')
    assert old.data['site']['kind']=='slurm' and old.legacy_profile=='desktop'
    explicit=resolve(tmp_path,config_path=path,site='desktop')
    assert explicit.data['site']['kind']=='desktop'


def test_light_to_heavy_rechecks_captured_and_new_policy(tmp_path):
    policy=write(tmp_path/'policy.json',{'schema':C.POLICY_SCHEMA,'restrictions':{'require_account':True,'allowed_accounts':['science']}})
    light=C.resolve(env=environment(tmp_path),system_policy_paths=[policy])
    with pytest.raises(ValueError):C.admission_config(light,system_policy_paths=[])
    admitted=C.resolve(env=environment(tmp_path),system_policy_paths=[policy],cli_overrides={'allocation':{'account':'science'}})
    assert C.admission_config(admitted,system_policy_paths=[])['execution']['startup']=='heavy'
    write(Path(policy),{'schema':C.POLICY_SCHEMA,'restrictions':{'deny_heavy':True}})
    with pytest.raises(ValueError,match='disables new Heavy'):C.admission_config(admitted,system_policy_paths=[policy])


def test_legacy_desktop_reload_does_not_gain_local_execution(tmp_path):
    path=write(tmp_path/'legacy.json',{'profiles':{'desktop':{'account':'workstation'}}})
    initial=resolve(tmp_path,config_path=path,legacy_profile='desktop')
    reloaded=C.reload_config(initial,env=environment(tmp_path),system_policy_paths=[])
    assert reloaded.legacy_profile=='desktop'
    assert reloaded.data['site']['kind']=='slurm'
    assert reloaded.data['execution']['desktop_execution']=='disabled'


def test_worker_policy_rechecks_reviewed_account_and_connection(tmp_path):
    policy=write(tmp_path/'policy.json',{'schema':C.POLICY_SCHEMA,'restrictions':{
        'require_account':True,'allowed_accounts':['science'],'allowed_transports':['ssh']}})
    request={'site_kind':'slurm','resources':{},'context':{
        'account':'science','partition':'cpu','transport':'ssh','host':'cluster-alias'}}
    assert C.worker_policy_check(request,system_policy_paths=[policy])==request
    with pytest.raises(ValueError):
        C.worker_policy_check({**request,'context':{**request['context'],'account':'wrong'}},system_policy_paths=[policy])


def test_legacy_child_explicit_empty_host_and_account_clear_parent(tmp_path):
    path=write(tmp_path/'legacy.json',{'host':'carc.invalid','account':'cluster-account','ssh_user':'remote-user',
        'profiles':{'desktop':{'host':'','account':''},'inherited':{'theme':'mono'}}})
    desktop=resolve(tmp_path,config_path=path,legacy_profile='desktop')
    assert desktop.data['site']['kind']=='slurm'
    assert desktop.data['connection']=={'transport':'local','host':'','user':'','credential_reference':'','ssh_options':[]}
    assert desktop.data['allocation']['account']==''
    inherited=resolve(tmp_path,config_path=path,legacy_profile='inherited')
    assert inherited.data['connection']['host']=='carc.invalid'
    assert inherited.data['allocation']['account']=='cluster-account'


def test_site_policy_schema_matches_numeric_paths_and_example():
    import jsonschema
    root=Path(__file__).resolve().parents[1]
    schema=json.loads((root/'docs/schemas/raven-site-policy.schema.json').read_text())
    validator=jsonschema.Draft202012Validator(schema)
    validator.check_schema(schema)
    example=json.loads((root/'docs/examples/raven/site-policy.json').read_text())
    validator.validate(example);C._policy(example)
    restrictions=schema['properties']['restrictions']['properties']
    for path,value in C.flatten(C.DEFAULTS).items():
        if not isinstance(value,(int,float)) or isinstance(value,bool):continue
        category='floors' if path.startswith('collection.') and path.endswith('_seconds') else 'ceilings'
        assert path in restrictions[category]['properties']
        field=restrictions[category]['properties'][path]
        # Test representative endpoints against the real numeric validator.
        low=field['minimum'];high=field['maximum']
        if field['type']=='integer':low=max(1,int(low)) if low else 0
        for limit in (low,high):
            document={'schema':C.POLICY_SCHEMA,'restrictions':{category:{path:limit}}}
            validator.validate(document);C._policy(document)


@pytest.mark.parametrize('restriction',[
    {'unknown':True},{'floors':{'resources.heavy.memory_mib':1}},
    {'ceilings':{'collection.queue_seconds':60}},{'floors':{'collection.queue_seconds':0}},
    {'ceilings':{'resources.heavy.max_workers':1000}},{'require_account':'yes'},
    {'allowed_accounts':[True]},{'allowed_accounts':['']},
])
def test_site_policy_schema_and_runtime_reject_invalid_restrictions(restriction):
    import jsonschema
    root=Path(__file__).resolve().parents[1]
    schema=json.loads((root/'docs/schemas/raven-site-policy.schema.json').read_text())
    document={'schema':C.POLICY_SCHEMA,'restrictions':restriction}
    with pytest.raises(jsonschema.ValidationError):jsonschema.Draft202012Validator(schema).validate(document)
    with pytest.raises(ValueError):C._policy(document)
