"""Real filesystem fault tests: originals and historical evidence never change."""
import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from raven import raven_migration as M
from raven import raven_config as C


def test_migration_legacy_alias_preserves_exact_module_identity(monkeypatch):
    from raven.installation import config_migration
    assert M is config_migration
    marker = object()
    monkeypatch.setattr(M, "_migration_alias_probe", marker, raising=False)
    assert config_migration._migration_alias_probe is marker


@pytest.fixture
def migration(tmp_path):
    old=tmp_path/'old-config';old.mkdir(mode=0o700)
    state=tmp_path/'old-state';state.mkdir(mode=0o700)
    (old/'config.json').write_text(json.dumps({'theme':'mono','intervals':{'jobs':.5}}))
    (state/'groups.json').write_text('{"schema":"raven.groups/v1","jobs":["123"]}')
    return dict(source_config=old,source_state=state,destination_config=tmp_path/'new-config'/'config.json',destination_state=tmp_path/'new-state')


def originals(migration):
    return {str(path):path.read_bytes() for root in ('source_config','source_state') for path in migration[root].rglob('*') if path.is_file() and not path.is_symlink()}


def test_plan_is_bounded_read_only_and_explicit(migration):
    initial=originals(migration);value=M.plan(**migration)
    assert len(value['files'])==2
    assert not migration['destination_state'].exists()
    assert originals(migration)==initial
    assert value['converted_config']['collection']['queue_seconds']==60
    assert value['converted_config']['ui']['theme']=='mono'


@pytest.mark.parametrize('missing', ['source_config', 'source_state'])
def test_new_plan_requires_both_explicit_source_roots(migration, missing):
    arguments = {key: value for key, value in migration.items() if key != missing}
    initial = originals(migration)
    with pytest.raises(ValueError, match='explicit --source-config and --source-state'):
        M.plan(**arguments)
    assert originals(migration) == initial
    assert not migration['destination_state'].exists()


def test_versioned_source_config_preserves_supported_settings(migration):
    source = migration['source_config'] / 'config.json'
    source.write_text(json.dumps({'schema': C.SCHEMA, 'profile': 'lab',
                                 'allocation': {'account': 'research', 'partition': 'local'},
                                 'ui': {'theme': 'mono'}}))
    original = source.read_bytes()
    value = M.plan(**migration)
    assert value['converted_config']['profile'] == 'lab'
    assert value['converted_config']['allocation']['account'] == 'research'
    assert value['converted_config']['allocation']['partition'] == 'local'
    assert value['converted_config']['ui']['theme'] == 'mono'
    assert source.read_bytes() == original


def test_unknown_versioned_source_config_is_not_silently_replaced(migration):
    source = migration['source_config'] / 'config.json'
    source.write_text(json.dumps({'schema': 'unrelated.config/v1', 'ui': {'theme': 'mono'}}))
    original = source.read_bytes()
    with pytest.raises(ValueError, match='Unsupported configuration schema'):
        M.plan(**migration)
    assert source.read_bytes() == original
    assert not migration['destination_state'].exists()


def test_apply_restart_and_rollback_keep_originals_and_schemas(migration):
    initial=originals(migration);value=M.plan(**migration)
    result=M.apply(value)
    assert result['files']==3 and result['state']=='complete'
    for record in value['files']:
        assert Path(record['destination']).read_bytes()==initial[record['source']]
    assert M.apply(value)['files']==3
    assert originals(migration)==initial
    assert C.read_file(migration['destination_config'])['schema']==C.SCHEMA
    journal=Path(result['journal']);assert journal.stat().st_mode&0o777==0o600
    assert M.rollback(journal)['removed']==3
    assert M.rollback(journal)['removed']==0
    assert originals(migration)==initial
    assert not migration['destination_config'].exists()


def test_existing_raven_config_never_merged_or_replaced(migration):
    target=migration['destination_config'];target.parent.mkdir(mode=0o700)
    target.write_text('{"schema":"raven.config/v1","ui":{"theme":"dark"}}')
    initial=target.read_bytes();value=M.plan(**migration)
    assert value['converted_config'] is None and value['conflicts']
    result=M.apply(value);M.rollback(result['journal'])
    assert target.read_bytes()==initial


def test_active_sources_changed_after_plan_fail_without_copies(migration):
    value=M.plan(**migration)
    (migration['source_state']/'groups.json').write_text('changed')
    with pytest.raises(ValueError,match='Legacy source changed'):M.apply(value)
    assert not (migration['destination_state']/'legacy-imports').exists()


def test_rollback_refuses_modified_or_replaced_output(migration):
    result=M.apply(M.plan(**migration));target=migration['destination_config']
    target.write_bytes(target.read_bytes()+b' ')
    with pytest.raises(ValueError,match='changed'):M.rollback(result['journal'])
    assert target.exists()


@pytest.mark.parametrize('rollback', [False,True])
def test_crash_after_config_publication_before_journal_append_recovers(migration,monkeypatch,rollback):
    value=M.plan(**migration);writer=M._journal_write;target=str(migration['destination_config'])
    def interrupted(path,document):
        if any(item['path']==target for item in document.get('outputs',[])):
            raise RuntimeError('simulated process death after link')
        return writer(path,document)
    monkeypatch.setattr(M,'_journal_write',interrupted)
    with pytest.raises(RuntimeError):M.apply(value)
    assert migration['destination_config'].exists()
    monkeypatch.setattr(M,'_journal_write',writer)
    journal=migration['destination_state']/'migrations'/(value['id']+'.json')
    if rollback:
        assert M.rollback(journal)['removed']==3
        assert not migration['destination_config'].exists()
    else:
        assert M.apply(value)['files']==3
        M.rollback(journal)


def test_crash_after_intent_before_link_can_resume(migration,monkeypatch):
    value=M.plan(**migration);link=M.os.link
    def fail(source,target,**kwargs):
        if str(target)==str(migration['destination_config']):raise RuntimeError('before publication')
        return link(source,target,**kwargs)
    monkeypatch.setattr(M.os,'link',fail)
    with pytest.raises(RuntimeError):M.apply(value)
    monkeypatch.setattr(M.os,'link',link)
    assert M.apply(value)['files']==3


def test_pending_same_content_replacement_is_not_adopted(migration,monkeypatch):
    value=M.plan(**migration);writer=M._journal_write;target=migration['destination_config']
    def interrupted(path,document):
        if any(item['path']==str(target) for item in document.get('outputs',[])):raise RuntimeError('crash')
        return writer(path,document)
    monkeypatch.setattr(M,'_journal_write',interrupted)
    with pytest.raises(RuntimeError):M.apply(value)
    raw=target.read_bytes();alternate=target.parent/'replacement';alternate.write_bytes(raw)
    os.replace(alternate,target);monkeypatch.setattr(M,'_journal_write',writer)
    with pytest.raises(ValueError,match='not proven'):M.apply(value)


def test_fifo_symlinks_runtime_and_default_history_skipped(migration,tmp_path):
    state=migration['source_state']
    os.mkfifo(state/'fifo');(state/'linked').symlink_to(state/'groups.json')
    (state/'runtime').mkdir();(state/'runtime'/'worker.pid').write_text('42')
    (state/'history.jsonl').write_text('{"schema":"raven.event/v1"}\n')
    value=M.plan(**migration)
    assert {record['path'] for record in value['files']}=={'config.json','groups.json'}
    explicit=M.plan(**migration,include_history=True)
    assert 'history.jsonl' in {record['path'] for record in explicit['files']}
    assert 'worker.pid' not in {record['path'] for record in explicit['files']}


def test_destination_symlink_and_overlapping_roots_rejected(migration,tmp_path):
    initial=originals(migration)
    with pytest.raises(ValueError,match='separate'):M.plan(**{**migration,'destination_state':migration['source_state']/'inside'})
    migration['destination_state'].symlink_to(tmp_path)
    with pytest.raises(ValueError,match='symlinks'):M.apply(M.plan(**migration))
    assert originals(migration)==initial
    assert migration['destination_state'].is_symlink()
    assert not (tmp_path/'migrations').exists()
    assert not migration['destination_config'].parent.exists()


def test_tampered_plan_rejected_before_filesystem_mutation(migration):
    value=M.plan(**migration);value['files'][0]['destination']='/tmp/not-allowed'
    with pytest.raises(ValueError,match='plan changed'):M.apply(value)
    assert not migration['destination_state'].exists()


def test_inventory_budget_marks_truncation(migration):
    root=migration['source_state']
    for i in range(5):(root/f'{i}.txt').write_text('one')
    value=M.inventory(root,max_files=2)
    assert value['limited'] and len(value['files'])==2


def test_migration_cli_plan_apply_and_rollback(migration,tmp_path,capsys):
    arguments=[]
    for name,path in migration.items():arguments.extend(['--'+name.replace('_','-'),str(path)])
    saved=tmp_path/'private-plan'/'review.json'
    assert M.cli(['--plan','--save-plan',str(saved),*arguments])==0
    value=json.loads(capsys.readouterr().out);assert value['schema']==M.PLAN_SCHEMA
    assert M.cli(['--apply','--plan-file',str(saved)])==0
    result=json.loads(capsys.readouterr().out)
    assert M.cli(['--rollback',result['journal']])==0
    assert json.loads(capsys.readouterr().out)['state']=='rolled-back'


def add_ui(migration,scope=None):
    content={'theme':'dark','tab':'jobs','bookmarks':{'secret-path':[1]},'gpu':True,
        'workbench':{'terminal_ui':{'sessions':['do-not-adopt']},'table_ui':{
            'widths':{'jobs':{'name':30}},'manual_groups':{'version':1,'groups':[{'id':'keep-identity'}]},'collapsed':['group1']}}}
    if scope is not None:content['forecast_state']={'version':1,'scope':scope,'observations':[]}
    (migration['source_state']/'ui.json').write_text(json.dumps(content))


def scope():
    return {'backend':'local','host':'test-host','profile':'default-slurm','uid':os.getuid(),'user':'test-user','ssh_user':'','ssh_options_sha256':''}


def test_activation_receipt_restores_only_matching_group_identity(migration):
    add_ui(migration,scope());value=M.plan(**migration);result=M.apply(value)
    ui,status=M.import_ui(migration['destination_state'],scope())
    assert status['groups_imported'] and ui['theme']=='dark'
    assert ui['workbench']['table_ui']['manual_groups']['groups'][0]['id']=='keep-identity'
    assert 'terminal_ui' not in ui['workbench'] and 'bookmarks' not in ui and 'gpu' not in ui
    M.rollback(result['journal'])
    assert M.import_ui(migration['destination_state'],scope())[0]=={}


def test_unknown_connection_imports_appearance_only(migration):
    add_ui(migration,scope());M.apply(M.plan(**migration))
    ui,status=M.import_ui(migration['destination_state'],{**scope(),'host':'other-cluster'})
    assert ui['theme']=='dark' and not status['groups_imported']
    assert 'manual_groups' not in ui['workbench']['table_ui']
    assert 'collapsed' not in ui['workbench']['table_ui']
    assert ui['workbench']['table_ui']['widths']['jobs']['name']==30


def test_changed_config_cannot_adopt_old_group_authority(migration):
    add_ui(migration,scope());M.apply(M.plan(**migration))
    target=migration['destination_config'];target.write_bytes(target.read_bytes()+b' ')
    ui,status=M.import_ui(migration['destination_state'],scope())
    assert ui['theme']=='dark' and not status['groups_imported']


def test_changed_archived_preferences_never_used(migration):
    add_ui(migration,scope());value=M.plan(**migration);M.apply(value)
    target=next(record['destination'] for record in value['files'] if record['path']=='ui.json')
    Path(target).write_text('{"theme":"evil"}')
    ui,status=M.import_ui(migration['destination_state'],scope())
    assert ui=={} and status['state']=='unavailable'


def test_oversized_ui_is_archived_without_startup_import(migration):
    (migration['source_state']/'ui.json').write_text(json.dumps({'padding':'x'*(M.UI_IMPORT_BYTES+1)}))
    value=M.plan(**migration)
    assert value['activation'] is None and not value['ui_candidates']
    result=M.apply(value);assert result['files']==4
    assert M.import_ui(migration['destination_state'],scope())[0]=={}
