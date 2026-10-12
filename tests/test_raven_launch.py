"""Bootstrap tests never request a real allocation or read the user's state."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from raven import raven_launch as L
from raven import raven_config as C


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ('RAVEN_CONFIG','RAVEN_PROFILE','RAVEN_STATE_DIR','RAVEN_SITE_POLICY'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('HOME',str(tmp_path))
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'config'))
    monkeypatch.setenv('XDG_STATE_HOME',str(tmp_path/'state'))
    monkeypatch.setattr(C,'SYSTEM_POLICIES',())


def test_light_plan_is_pure_and_does_not_load_dashboard(tmp_path,capsys):
    program='from raven.raven_launch import light_main; import sys; assert light_main(["--plan"]) == 0; assert "raven.cli" not in sys.modules; assert "raven.raven_allocation" not in sys.modules'
    result=subprocess.run([sys.executable,'-c',program],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['mutations']==0
    assert not (tmp_path/'state').exists()


def test_heavy_plan_never_requests_allocation(monkeypatch,capsys):
    manager=SimpleNamespace(plan=Mock(return_value={'backend':'desktop'}),request=Mock(side_effect=AssertionError))
    monkeypatch.setattr(L,'_allocation',lambda _:manager)
    assert L.heavy_main(['--site','desktop','--plan'])==0
    assert json.loads(capsys.readouterr().out)['intent']=='heavy'
    manager.request.assert_not_called()


@pytest.mark.parametrize('args', [['--fake'],['--site','desktop','--fake']])
def test_fake_heavy_rejected_before_manager(monkeypatch,args,capsys):
    monkeypatch.setattr(L,'_allocation',Mock(side_effect=AssertionError))
    assert L.heavy_main(args)==2
    assert 'cannot request a real allocation' in capsys.readouterr().err


@pytest.mark.parametrize('entry', [L.light_main,L.heavy_main])
@pytest.mark.parametrize('prefix', [[],['--site','desktop']])
def test_nested_help_preserved_and_never_allocates(monkeypatch,entry,prefix):
    from raven import cli
    dispatch=Mock(return_value=0)
    monkeypatch.setattr(cli,'main',dispatch)
    monkeypatch.setattr(L,'_allocation',Mock(side_effect=AssertionError('help cannot allocate')))
    assert entry(prefix+['terminal','--help'])==0
    assert dispatch.call_args.args[0]==['terminal','--help']
    assert dispatch.call_args.kwargs['raven'].data['execution']['startup']=='light'


def test_nested_config_is_not_bootstrap_config(monkeypatch):
    from raven import cli
    dispatch=Mock(return_value=0); monkeypatch.setattr(cli,'main',dispatch)
    assert L.light_main(['--site','desktop','report','init','--config','project.json'])==0
    assert dispatch.call_args.args[0]==['report','init','--config','project.json']


def test_legacy_profile_desktop_preserves_slurm(monkeypatch,tmp_path,capsys):
    from raven import cli
    source=tmp_path/'legacy.json'
    source.write_text(json.dumps({'profiles':{'desktop':{'account':'workstation','research_roots':['/tmp/project']}}}))
    dispatch=Mock(return_value=0); monkeypatch.setattr(cli,'main',dispatch)
    assert L.light_main(['--config',str(source),'--profile','desktop','--once'])==0
    value=dispatch.call_args.kwargs['raven']
    assert value.legacy_profile=='desktop'
    assert value.data['site']['kind']=='slurm'
    assert value.data['allocation']['account']=='workstation'
    assert dispatch.call_args.args[0]==['--once']
    assert '--profile is a legacy alias' in capsys.readouterr().err


def test_heavy_wait_keeps_ui_import_after_ready_and_reuses_manager(monkeypatch):
    from raven import cli
    events=[]
    manager=SimpleNamespace(worker=SimpleNamespace(ready=False),request=lambda **kw:events.append('request'),
        shutdown=lambda:events.append('shutdown'))
    states=iter(['preparing','pending','starting','heavy'])
    def tick():
        manager.state=next(states);manager.worker.ready=manager.state=='heavy';events.append(manager.state)
    manager.tick=tick;manager.snapshot=lambda:{'state':manager.state}
    monkeypatch.setattr(L,'_allocation',lambda _:manager)
    monkeypatch.setattr(L,'configure_placement',lambda *a:None)
    monkeypatch.setattr(L.time,'sleep',lambda _:None)
    def main(args,**kwargs):
        assert manager.worker.ready and kwargs['raven_allocation'] is manager
        events.append('dashboard');return 7
    monkeypatch.setattr(cli,'main',main)
    assert L.heavy_main(['--site','desktop'])==7
    assert events==['request','preparing','pending','starting','heavy','dashboard','shutdown']


@pytest.mark.parametrize('state', [{'state':'light','error':'denied'},{'state':'reconciling','cleanup_uncertain':True}])
def test_startup_failure_cleans_owned_manager_and_never_opens_ui(monkeypatch,state):
    from raven import cli
    manager=SimpleNamespace(request=Mock(),tick=Mock(),snapshot=lambda:state,shutdown=Mock())
    monkeypatch.setattr(L,'_allocation',lambda _:manager)
    monkeypatch.setattr(L,'configure_placement',lambda *a:None)
    main=Mock(side_effect=AssertionError);monkeypatch.setattr(cli,'main',main)
    assert L.heavy_main(['--site','desktop'])==2
    manager.shutdown.assert_called_once();main.assert_not_called()


def test_write_config_is_explicit_private_and_no_overwrite(tmp_path,capsys):
    target=tmp_path/'private'/'raven.json'
    assert L.light_main(['--site','desktop','--write-config',str(target)])==0
    original=target.read_bytes()
    assert target.stat().st_mode&0o777==0o600
    assert L.light_main(['--write-config',str(target)])==2
    assert target.read_bytes()==original


def test_doctor_is_read_only_and_does_not_request(monkeypatch,capsys):
    monkeypatch.setattr(L,'_allocation',Mock(side_effect=AssertionError))
    assert L.heavy_main(['--site','desktop','doctor'])==0
    assert json.loads(capsys.readouterr().out)['schema']=='raven.doctor/v1'


def test_compute_interface_and_light_reuse_fail_closed(capsys):
    assert L.light_main(['--interface','compute'])==2
    assert L.light_main(['--allocation','123'])==2


@pytest.mark.parametrize('entry',[L.light_main,L.heavy_main])
def test_version_never_allocates_or_resolves_configuration(monkeypatch,entry,capsys):
    monkeypatch.setattr(L,'resolve',Mock(side_effect=AssertionError('version resolves nothing')))
    monkeypatch.setattr(L,'_allocation',Mock(side_effect=AssertionError))
    assert entry(['--version'])==0
    assert capsys.readouterr().out.startswith('Raven ')


@pytest.mark.parametrize('arguments', [['-m','raven'],[str(Path(__file__).resolve().parents[1]/'raven')]])
def test_module_and_package_directory_version_are_clean_entry_points(arguments,monkeypatch,tmp_path):
    monkeypatch.setenv('RAVEN_CONFIG',str(tmp_path/'missing.json'))
    result=subprocess.run([sys.executable,*arguments,'--version'],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert result.stdout.startswith('Raven ')
    assert result.stderr==''


def test_explicit_reuse_waits_for_opaque_receipt(monkeypatch,tmp_path):
    resolved=C.resolve(site='desktop',env={'HOME':str(tmp_path)},system_policy_paths=[])
    review=Mock();request=Mock()
    manager=SimpleNamespace(request_reuse_review=review,request=request,tick=Mock(),worker=SimpleNamespace(ready=True))
    manager.snapshot=lambda:{'state':'heavy','reuse_review':{'state':'ready','token':'opaque-receipt','plan':{'identity':'123'}}}
    L._wait_heavy(manager,resolved,'123')
    review.assert_called_once_with('123')
    request.assert_called_once_with(reuse='opaque-receipt',plan={'identity':'123'})


def test_explicit_legacy_account_resolves_before_site_policy(monkeypatch,tmp_path,capsys):
    policy=tmp_path/'policy.json'
    policy.write_text(json.dumps({'schema':C.POLICY_SCHEMA,'restrictions':{'require_account':True,'allowed_accounts':['science']}}))
    legacy=tmp_path/'legacy.json'
    legacy.write_text(json.dumps({'profiles':{'carc':{'account':''}}}))
    # A plan is read-only: prove the canonical account passed policy before the
    # allocation adapter sees it, without starting a real worker or scheduler.
    manager=SimpleNamespace(plan=Mock(return_value={'status':'review-only'}))
    def allocation(resolved):
        assert resolved.data['allocation']['account']=='science'
        assert resolved.legacy_profile=='carc'
        return manager
    monkeypatch.setattr(L,'_allocation',allocation)
    assert L.heavy_main(['--config',str(legacy),'--profile','carc','--account','science',
                         '--site-policy',str(policy),'--plan'])==0
    result=json.loads(capsys.readouterr().out)
    assert result['configuration']['effective']['allocation']['account']=='science'
    assert result['configuration']['origins']['allocation.account']=='CLI'


@pytest.mark.parametrize('tail', [['run','resubmit','123','--account','other'],['report','example','--account','other']])
def test_nested_account_stays_with_its_command(monkeypatch,tail):
    from raven import cli
    dispatch=Mock(return_value=0);monkeypatch.setattr(cli,'main',dispatch)
    assert L.light_main(['--account','science',*tail])==0
    assert dispatch.call_args.args[0]==tail
    assert dispatch.call_args.kwargs['raven'].data['allocation']['account']=='science'
