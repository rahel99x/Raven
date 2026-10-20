from dataclasses import dataclass
import json
import os
from pathlib import Path
import socket
import threading
from types import SimpleNamespace

import pytest

from raven import terminal_bridge as B, terminal_fusion as F


def event(kind, session='s', **kwargs):
    return {'event':kind, 'session':session, 'received':100., 'monotonic':10., **kwargs}


def block(store, *, session='s', text='output', command='', status=0):
    start = store.observe(event('start', session, command=command))
    store.observe(event('end', session, status=status, received=102., monotonic=12.), output=text)
    return start['id']


def test_private_bridge_roundtrip(tmp_path):
    bridge = B.Bridge(tmp_path)
    try:
        env = bridge.register('session')
        B.report('start', env=env, cwd=str(tmp_path), command='echo example')
        messages = bridge.poll()
        assert len(messages) == 1
        assert messages[0]['cwd'] == str(tmp_path)
        assert messages[0]['provenance'] == 'shell observation'
        assert 'token' not in messages[0]
        assert bridge.directory.stat().st_mode & 0o777 == 0o700
        assert os.lstat(bridge.path).st_mode & 0o777 == 0o600
    finally: bridge.close()
    assert not bridge.directory.exists()
    assert bridge.poll() == []


def test_long_multibyte_bridge_parent_uses_private_short_socket_root(tmp_path):
    parent = tmp_path / ("界" * 35)
    parent.mkdir()
    bridge = B.Bridge(parent)
    directory = bridge.directory
    try:
        assert len(os.fsencode(bridge.path)) <= 103
        assert directory.parent != parent and list(parent.iterdir()) == []
        assert directory.stat().st_mode & 0o777 == 0o700
        assert os.lstat(bridge.path).st_mode & 0o777 == 0o600
        env = bridge.register("long-parent")
        B.report("prompt", env=env)
        assert bridge.poll()[0]["session"] == "long-parent"
    finally:
        bridge.close()
    assert not directory.exists() and list(parent.iterdir()) == []


@pytest.mark.parametrize('change', [
    {'token':'wrong'}, {'session':'other'}, {'event':'cancel-job'}, {'schema':'other'},
    {'command':'x'*2049}, {'cwd':'bad\x00path'}, {'status':True}, {'status':300},
    {'status':'0'}, {'session':[]}, {'token':None}, {'job':3},
])
def test_bridge_rejects_untrusted_invalid_fields(tmp_path, change):
    bridge = B.Bridge(tmp_path)
    env = bridge.register('s')
    value = {'schema':B.SCHEMA, 'session':'s', 'token':env['RAVEN_BRIDGE_TOKEN'], 'event':'end', 'status':0, **change}
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as client:
            client.sendto(json.dumps(value).encode(),bridge.path)
        assert bridge.poll() == []
        assert bridge.rejected == 1
    finally: bridge.close()


@pytest.mark.parametrize('data', [b'not JSON', b'[]', b'null', b'\xff', b'x'*(B.MAX_MESSAGE+1)])
def test_bridge_malformed_frames_are_bounded(tmp_path, data):
    bridge = B.Bridge(tmp_path)
    bridge.register('s')
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as client: client.sendto(data,bridge.path)
        assert bridge.poll() == [] and bridge.rejected == 1
    finally: bridge.close()


def test_bridge_revoked_session_cannot_report(tmp_path):
    bridge = B.Bridge(tmp_path)
    env = bridge.register('s')
    bridge.unregister('s')
    try:
        B.report('prompt', env=env)
        assert bridge.poll() == []
    finally: bridge.close()


def test_bridge_max_sessions_and_tick_work(tmp_path):
    bridge = B.Bridge(tmp_path)
    try:
        for index in range(32): bridge.register(str(index))
        with pytest.raises(ValueError, match='limit'): bridge.register('overflow')
        env = bridge.register('0')
        for _ in range(4): B.report('prompt', env=env)
        assert len(bridge.poll(2)) == 2
        assert len(bridge.poll(2)) == 2
    finally: bridge.close()


def test_report_rejects_public_or_missing_socket(tmp_path):
    with pytest.raises(ValueError, match='no Raven bridge'): B.report('prompt', env={})
    bridge = B.Bridge(tmp_path)
    env = bridge.register('s')
    os.chmod(bridge.path,0o666)
    try:
        with pytest.raises(ValueError, match='private socket'): B.report('prompt',env=env)
    finally: bridge.close()


@pytest.mark.parametrize('shell', ['bash','zsh','fish'])
def test_shell_hooks_are_opt_in_idempotent_and_do_not_capture_history(shell):
    text = B.shell_hook(shell)
    assert '_RAVEN_HOOK_INSTALLED' in text
    assert '_raven_report start' in text
    assert '_raven_report end' in text
    assert 'history ' not in text
    assert 'DEBUG' not in text


def test_command_blocks_clocks_hidden_notice_and_exact_text():
    store=F.FusionStore();store.register('s',host='host',cwd='/project')
    first=store.observe(event('start',command='echo test'))
    result=store.observe(event('end',status=1,received=102.,monotonic=12.),output='secret-output',visible=False)
    assert result['duration']==2 and result['status']==1
    assert result['coverage'].startswith('rendered screen observation')
    assert result['clock']['remote_skew']=='unknown'
    assert 'secret-output' not in json.dumps(list(store.notices))
    assert store.get(first['id'])['output']=='secret-output'
    assert store.failures()[0]['cause'].startswith('unknown')
    store.dismiss(first['id']);assert store.failures()==[]


def test_command_completion_rejects_status_without_losing_current():
    store=F.FusionStore();store.register('s')
    first=store.observe(event('start'))
    with pytest.raises(ValueError):store.observe(event('end',status='0'))
    assert store.sessions['s']['current']==first['id']


def test_command_metadata_never_automatically_records_input():
    store=F.FusionStore();store.register('s')
    key=block(store,text='rendered text')
    assert store.get(key)['command']==''
    assert 'input' not in store.get(key)


def test_evidence_eviction_and_pins_have_independent_immutable_snapshot():
    store=F.FusionStore(max_blocks=2);store.register('s')
    first=block(store,text='first');pin=store.bookmark(first,'label',metrics=[{'cpu':2}])
    block(store,text='second');block(store,text='third')
    with pytest.raises(ValueError,match='expired'):store.get(first)
    assert store.evicted==1 and store.pins[pin['id']]['block']['output']=='first'
    pin['block']['output']='changed'
    assert store.pins[pin['id']]['block']['output']=='first'


def test_evidence_byte_budget_and_output_limits():
    store=F.FusionStore(max_bytes=1024);store.register('s')
    key=block(store,text='x'*2048)
    assert store.bytes==0 and store.evicted==1
    with pytest.raises(ValueError):store.get(key)


def test_submission_must_match_unique_exact_current_identity():
    store=F.FusionStore();store.register('s')
    job={'id':'123','submit':'date','cluster':'cluster','start':'now','user':'me'}
    with pytest.raises(ValueError,match='not yet'):store.observe(event('submission',job='123'),jobs=[])
    with pytest.raises(ValueError,match='exact'):store.observe(event('submission',job='123;cluster;evil'),jobs=[job])
    store.observe(event('submission',job='123;cluster'),jobs=[job])
    assert store.sessions['s']['jobs']==[F.job_identity(job)]
    with pytest.raises(ValueError):store.observe(event('submission',job='123'),jobs=[job,job])
    key=block(store)
    assert store.get(key)['jobs'][0]['submit']=='date'


def test_paths_resolve_against_observed_host_cwd_without_opening(tmp_path):
    store=F.FusionStore();store.register('s',host='remote',cwd='/work')
    store.observe(event('project',project='project'))
    store.observe(event('artifact',artifact='results/metrics.json'))
    assert store.sessions['s']['project']=='/work/project'
    artifact=store.sessions['s']['artifacts'][0]
    assert artifact['path']=='/work/results/metrics.json' and artifact['host']=='remote'
    assert not (tmp_path/'results').exists()


def test_unknown_cwd_does_not_resolve_using_raven_directory():
    store=F.FusionStore();store.register('s')
    with pytest.raises(ValueError,match='absolute'):store.observe(event('artifact',artifact='relative'))


def test_search_filter_and_output_comparison():
    store=F.FusionStore();store.register('s',host='host')
    left=block(store,text='one\ntwo',command='old',status=1)
    right=block(store,text='one \nthree',command='new')
    assert [b['id'] for b in store.search('old',host='host',status=1)]==[left]
    assert not store.search('old',host='other')
    diff=store.compare(left,right,normalize='trailing-space')
    assert '-two' in diff and '+three' in diff and '-one' not in diff
    with pytest.raises(ValueError):store.compare(left,right,normalize='arbitrary-shell-command')


def test_comparison_rejects_pathological_line_counts():
    store=F.FusionStore();store.register('s')
    left=block(store,text='a\n'*1025);right=block(store,text='b')
    with pytest.raises(ValueError,match='shorter'):store.compare(left,right)


def test_redacted_export_no_input_or_artifact_file_and_exclusive(tmp_path):
    store=F.FusionStore();store.register('s')
    key=block(store,text='token=abc\nprivate-value',command='password=def')
    store.capture_environment('before',{'API_TOKEN':'secret','PATH':'/secret-path','LOADEDMODULES':'python/3.12'})
    path=tmp_path/'bundle.json'
    result=store.export(path,[key],exclusions=['private-value','/secret-path'])
    text=path.read_text()
    assert 'abc' not in text and 'def' not in text and 'private-value' not in text and '/secret-path' not in text
    assert 'API_TOKEN' not in text and result['bytes']==path.stat().st_size
    assert path.stat().st_mode & 0o777==0o600
    with pytest.raises(FileExistsError):store.export(path,[key])


def test_environment_diff_distinguishes_missing_values():
    store=F.FusionStore()
    store.capture_environment('before',{'LANG':'C','SLURM_JOB_ID':'1'})
    store.capture_environment('after',{'LANG':'en_US'})
    diff=store.environment_diff('before','after')
    assert {'key':'SLURM_JOB_ID','before':'1','after':None} in diff
    with pytest.raises(ValueError):store.environment_diff('before','missing')


def test_read_only_share_has_no_input_endpoint_and_is_private(tmp_path):
    path=tmp_path/'share.json'
    result=F.publish_share(path,'token=123\nhello',session='s')
    assert result['access'].startswith('read only')
    data=F.read_share(path)
    assert data['text']=='token=[REDACTED]\nhello'
    assert data['access']=='read only'
    path.chmod(0o644)
    with pytest.raises(ValueError):F.read_share(path)


def test_share_rejects_malformed_and_oversize(tmp_path):
    path=tmp_path/'share'
    path.write_text('{"schema":"other"}');path.chmod(0o600)
    with pytest.raises(ValueError):F.read_share(path)
    path.write_bytes(b'x'*(F.MAX_OUTPUT*8+1))
    with pytest.raises(ValueError):F.read_share(path)


def test_snapshot_text_reads_segments_without_styles():
    value={'lines':[[['hello','green'],[' world','bold']],[['second','red']]]}
    assert F._snapshot_text(value)=='hello world\nsecond'


def test_bounded_session_registration_and_unknown_events():
    store=F.FusionStore()
    for index in range(32):store.register(str(index))
    with pytest.raises(ValueError):store.register('overflow')
    with pytest.raises(ValueError):store.observe(event('start','unknown'))


@pytest.mark.parametrize('stamp',[True,float('inf'),float('nan'),'now'])
def test_invalid_observation_clocks_rejected(stamp):
    store=F.FusionStore();store.register('s')
    with pytest.raises(ValueError):store.observe(event('start',received=stamp))


def test_duplicate_start_marks_incomplete_without_fabricating_end():
    store=F.FusionStore();store.register('s')
    first=store.observe(event('start'))
    second=store.observe(event('start'))
    assert store.get(first['id'])['incomplete']
    assert store.get(first['id'])['ended'] is None
    assert first['id']!=second['id']


def app_for_ui(monkeypatch):
    from raven import terminal_ui
    app=SimpleNamespace(tab='jobs',mode='main',reports=[],errors=[])
    app.fail=app.errors.append
    monkeypatch.setattr(terminal_ui,'show_report',lambda app,lines,title='':app.reports.append((title,list(lines))))
    state=F.initialize(app);state['store'].register('s')
    return app,state


def test_ui_export_requires_fresh_preview_including_environment(tmp_path,monkeypatch):
    app,state=app_for_ui(monkeypatch)
    key=block(state['store'])
    assert F.run_command(app,['terminal','evidence-export',str(tmp_path/'bad')])
    assert app.errors and not (tmp_path/'bad').exists()
    app.errors.clear()
    F.run_command(app,['terminal','evidence-preview',key])
    state['store'].capture_environment('changed',{'LANG':'C'})
    F.run_command(app,['terminal','evidence-export',str(tmp_path/'stale')])
    assert 'changed after preview' in app.errors[-1]
    assert not (tmp_path/'stale').exists()
    F.run_command(app,['terminal','evidence-preview',key])
    F.run_command(app,['terminal','evidence-export',str(tmp_path/'good')])
    assert (tmp_path/'good').exists() and state['preview'] is None


@pytest.mark.parametrize('args',[
    ['blocks'],['block'],['evidence-search'],['bookmark'],['pins'],['timeline'],['failures'],['dismiss'],
    ['compare'],['evidence-preview'],['environment'],['share'],['share-read'],['record'],['replay'],
    ['hook'],['handoffs'],['handoff'],['job-link'],['evidence-help'],
])
def test_ui_commands_either_report_or_fail_without_crashing(monkeypatch,args):
    app,_=app_for_ui(monkeypatch)
    assert F.run_command(app,['terminal']+args)
    assert app.reports or app.errors


def test_job_navigation_revalidates_attempt_before_open(monkeypatch):
    app,state=app_for_ui(monkeypatch)
    before={'id':'1','submit':'old'}
    state['store'].observe(event('submission',job='1'),jobs=[before])
    app.store=SimpleNamespace(lock=threading.RLock(),jobs=[{'id':'1','submit':'new'}],finished=[])
    app.open_log=lambda _:pytest.fail('stale attempt must not open logs')
    F.run_command(app,['terminal','job-link','1'])
    assert 'no longer current' in app.errors[-1]


def test_metric_pin_reads_cached_data_only():
    calls=[]
    def publication(jid,**kwargs):
        calls.append((jid,kwargs));return [{'t':99,'cpu':1},{'t':101,'cpu':2},{'t':103,'cpu':3}]
    app=SimpleNamespace(store=SimpleNamespace(series_publication=publication))
    value={'started':100.,'ended':102.,'jobs':[{'id':'1'}]}
    assert F._metric_snapshot(app,value)==[{'job':'1','sample':{'t':101,'cpu':2}}]
    assert calls==[('1',{'memory_only':True})]


def test_environment_report_compares_declared_packages_and_modules(tmp_path):
    bridge=B.Bridge(tmp_path);env=bridge.register('s')
    store=F.FusionStore();store.register('s')
    try:
        B.report('environment',env=env,label='run1',environment={'LANG':'C','API_TOKEN':'do-not-keep'},packages={'numpy':'2.0'},modules={'cuda':'12'})
        report=bridge.poll()[0]
        assert 'API_TOKEN' not in report['environment']
        store.observe(report)
        B.report('environment',env=env,label='run2',environment={'LANG':'C'},packages={'numpy':'2.1'},modules={'cuda':'13'})
        store.observe(bridge.poll()[0])
        assert {'key':'package.numpy','before':'2.0','after':'2.1'} in store.environment_diff('run1','run2')
        assert {'key':'module.cuda','before':'12','after':'13'} in store.environment_diff('run1','run2')
        assert store.environments['run1']['package_coverage']=='supplied'
    finally:bridge.close()


def test_unregister_revokes_capability_without_forgetting_evidence(monkeypatch):
    app,_=app_for_ui(monkeypatch)
    for index in range(40):
        sid=str(index)
        F.register_session(app,sid)
        F.unregister_session(app,sid)
    key=block(F.initialize(app)['store'])
    F.unregister_session(app,'s')
    assert F.initialize(app)['store'].get(key)['id']==key
    F.close(app)


def test_background_file_export_does_not_write_or_retarget_before_completion(tmp_path,monkeypatch):
    app,state=app_for_ui(monkeypatch)
    from raven import terminal_ui
    pending=[]
    app.interactive=True
    app.say=lambda *_:None
    app.research=SimpleNamespace(start_task=lambda worker,done:pending.append((worker,done)) or True)
    key=block(state['store'])
    F.run_command(app,['terminal','evidence-preview',key])
    path=tmp_path/'bundle'
    F.run_command(app,['terminal','evidence-export',str(path)])
    assert not path.exists() and state['busy']
    before=len(app.reports)
    terminal_ui.initialize(app).focus_generation+=1
    worker,done=pending.pop()
    value=worker()
    assert path.exists()
    done(value)
    assert not state['busy'] and len(app.reports)==before
    assert state['last_result']['title']=='Evidence exported'


def test_complete_preview_does_not_hide_long_fields():
    store=F.FusionStore();store.register('s')
    key=block(store,text='x'*20000+'FINAL_SENTINEL')
    lines=F.preview_lines(store.bundle([key]))
    assert 'FINAL_SENTINEL' in '\n'.join(lines)
    assert max(map(len,lines))<=4000


def test_complete_preview_rejects_too_many_lines():
    store=F.FusionStore();store.register('s')
    key=block(store,text='x\n'*5000)
    with pytest.raises(ValueError,match='fewer'):F.preview_lines(store.bundle([key]))


def test_failure_artifacts_survive_session_close():
    store=F.FusionStore();store.register('s',cwd='/project')
    store.observe(event('artifact',artifact='logs/failure.log'))
    key=block(store,status=1)
    store.sessions.pop('s')
    assert store.failures()[0]['artifacts'][0]['path']=='/project/logs/failure.log'


def test_command_completion_records_explicit_output_coverage_limit():
    store=F.FusionStore();store.register('s')
    key=block(store,text='x'*(F.MAX_OUTPUT+1))
    assert store.get(key)['output_truncated'] is True


def test_absolute_helper_commands_quote_local_source_paths(monkeypatch,tmp_path):
    source=tmp_path/"helper dir's"/'terminal bridge.py'
    source.parent.mkdir();source.write_text('# local helper')
    monkeypatch.setattr(B,'__file__',str(source))
    import shlex
    command=B.helper_command('bash','hook','--shell','bash')
    assert shlex.split(command)[1:]==[str(source),'hook','--shell','bash']
    for shell in ('bash','zsh','fish'):
        script=B.shell_hook(shell)
        assert 'command raven ' not in script
        assert 'command grep' not in script
        assert '_raven_report' in script and 'terminal bridge.py' in script
    assert "dir\\'s" in B.helper_command('fish','report')


def test_hook_preparation_uses_absolute_helper_and_rejects_remote(monkeypatch):
    app,state=app_for_ui(monkeypatch)
    from raven import terminal_ui
    terminal=terminal_ui.initialize(app)
    terminal.active='s'
    snapshot={'kind':'local','metadata':{'host':socket.gethostname(),'shell':'/bin/bash'}}
    terminal.service=SimpleNamespace(snapshot=lambda _:snapshot)
    prepared=[]
    monkeypatch.setattr(terminal_ui,'prepare_input',lambda app,text,**kwargs:prepared.append(text))
    assert F.run_command(app,['terminal','hook','bash'])
    assert len(prepared)==1 and 'source <(' in prepared[0]
    assert 'raven terminal' not in prepared[0] and 'terminal_bridge.py' in prepared[0]
    snapshot.update(kind='ssh',metadata={'host':'other-host'})
    assert F.run_command(app,['terminal','hook','bash'])
    assert len(prepared)==1 and 'explicit local' in app.errors[-1]


def test_real_bash_hooks_work_without_raven_on_path_and_with_quoted_source(tmp_path,monkeypatch):
    pytest.importorskip('pyte')
    import shlex
    import time
    from raven.terminal_service import TerminalService
    # The standalone bridge uses stdlib only; its source may have spaces and apostrophes.
    source=tmp_path/"source dir's"/'terminal bridge.py'
    source.parent.mkdir()
    source.write_bytes(Path(B.__file__).read_bytes())
    monkeypatch.setattr(B,'__file__',str(source))
    hook=tmp_path/'setup hooks.sh'
    hook.write_text(B.shell_hook('bash'))
    empty_path=tmp_path/'empty-bin';empty_path.mkdir()
    bridge=B.Bridge()
    messages=[]
    try:
        with TerminalService() as service:
            sid=service.open(['/bin/bash','--noprofile','--norc','-i'],cwd=str(tmp_path),
                             env={'PATH':str(empty_path),'PS1':'HOOK_READY> ','PYTHONPATH':''},
                             env_factory=bridge.register)
            def text():
                return '\n'.join(''.join(segment[0] for segment in row) for row in service.snapshot(sid).get('lines',()))
            def until(predicate):
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    service.poll();messages.extend(bridge.poll())
                    if predicate():return
                    time.sleep(.005)
                pytest.fail('Hook state did not arrive: '+repr(messages)+' screen='+repr(text()))
            until(lambda:'HOOK_READY>' in text())
            assert service.send(sid,{'type':'text','text':'source '+shlex.quote(str(hook))+'\n'})
            until(lambda:any(message['event']=='prompt' for message in messages))
            messages.clear()
            assert service.send(sid,{'type':'text','text':'false\n'})
            until(lambda:any(message['event']=='end' and message.get('status')==1 for message in messages))
            assert any(message['event']=='start' for message in messages)
            assert all(message.get('command','')=='' for message in messages)
            assert all(message.get('cwd',str(tmp_path))==str(tmp_path) for message in messages)
            assert service.send(sid,{'type':'text','text':'printf "PRESERVED=%s\\n" "$?"\n'})
            until(lambda:'PRESERVED=1' in text())
            assert 'command not found' not in text()
    finally:
        bridge.close()


@pytest.mark.parametrize('actual,requested',[('/bin/sh','bash'),('/bin/bash','zsh'),('/bin/zsh','fish'),('/usr/bin/python','bash'),('','bash')])
def test_hook_preparation_refuses_unknown_or_mismatched_shell(monkeypatch,actual,requested):
    app,_=app_for_ui(monkeypatch)
    from raven import terminal_ui
    terminal=terminal_ui.initialize(app);terminal.active='s'
    snapshot={'kind':'local','metadata':{'host':socket.gethostname(),'shell':actual}}
    terminal.service=SimpleNamespace(snapshot=lambda _:snapshot)
    monkeypatch.setattr(terminal_ui,'prepare_input',lambda *_args,**_kwargs:pytest.fail('must not prepare a mismatched shell hook'))
    assert F.run_command(app,['terminal','hook',requested])
    assert app.errors
