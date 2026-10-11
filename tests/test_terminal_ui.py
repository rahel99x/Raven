"""Terminal pane ownership, precise selection, retained state, and bounded painting."""
from types import SimpleNamespace

import pytest

from tower import terminal_ui as T, layout as L, workspace_layout as W


class Service:
    def __init__(self):
        self.sessions = {'one': {'id':'one','label':'local:test','status':'running','seq':1,
            'lines':[[('one','text')],[('two','text')],[('three','text')]],'history':[],
            'cursor':{'x':0,'y':0,'visible':True},'modes':{}}}
        self.events=[]; self.resizes=[]; self.requests=[]; self.shutdowns=0
    def snapshot(self,sid): return self.sessions.get(sid,{})
    def send(self,sid,event): self.events.append((sid,event)); return True
    def resize(self,sid,rows,cols): self.resizes.append((sid,rows,cols))
    def poll(self): return False
    def request(self,sid,action,**kwargs): self.requests.append((sid,action,kwargs)); return True
    def close_session(self,sid): self.sessions.pop(sid,None)
    def shutdown(self): self.shutdowns+=1


def app(tmp_path, tab='jobs'):
    a=SimpleNamespace(tab=tab,mode='main',width=100,height=30,cfg={'clipboard':{'osc52':False,'tools':False}},
                      state_dir=str(tmp_path),messages=[],body_origin=4)
    a.say=lambda s:a.messages.append(s)
    a.fail=lambda s:a.messages.append('ERROR '+s)
    state=T.initialize(a); state.service=Service(); state.active='one'; state.modes[tab]='terminal'
    T._focus(a,True)
    T.render(a,40,12,x=55,y=6)
    return a


def test_initialization_and_render_do_not_start_shell(tmp_path,monkeypatch):
    a=SimpleNamespace(tab='jobs',mode='main',width=80,height=24)
    state=T.initialize(a)
    assert state.service is None and not T.showing(a)
    T.render(a,20,8)
    assert state.service is None
    T.restore(a,{'version':1,'modes':{'jobs':'terminal'}})
    T.render(a,20,8)
    assert state.service is None


@pytest.mark.parametrize('key',['q','y','g','u',':','esc','ctrl-c','ctrl-z','tab','f6','up'])
def test_shell_owns_tower_bindings(tmp_path,key):
    a=app(tmp_path); s=a.terminal_state
    assert T.handle_key(a,key)
    assert s.service.events[-1]==('one',{'type':'key','key':key})
    assert a.tab=='jobs' and a.mode=='main'


def test_explicit_escape_prefix_and_literal_delivery(tmp_path):
    a=app(tmp_path); s=a.terminal_state
    identity=T.input_identity(a)
    T.handle_key(a,'ctrl-]'); T.handle_key(a,'ctrl-]')
    assert s.service.events[-1][1]=={'type':'key','key':'ctrl-]'}
    T.handle_key(a,'ctrl-]'); T.handle_key(a,'t')
    assert not T.focused(a) and T.input_identity(a) is None
    assert s.focus_generation>identity[1]
    assert not T.handle_key(a,'q')


def test_hover_never_focuses_but_press_does(tmp_path):
    a=app(tmp_path); s=a.terminal_state; r=s.body_rect
    T.blur(a); generation=s.focus_generation
    T.handle_mouse(a,r.y,r.x,'move')
    assert not T.focused(a) and s.focus_generation==generation
    assert T.handle_mouse(a,r.y,r.x,'left')
    assert T.focused(a)
    assert T.handle_mouse(a,0,0,'release')
    assert s.capture is None


def test_terminal_capture_never_releases_to_other_pane(tmp_path):
    a=app(tmp_path); s=a.terminal_state; r=s.body_rect
    T.handle_mouse(a,r.y,r.x,'left')
    for coords in ((0,0),(29,99),(-10,-10)):
        assert T.handle_mouse(a,*coords,'drag')
    assert T.handle_mouse(a,0,0,'release')
    assert s.capture is None
    assert s.service.events[-1][1]['action']=='release'


def test_page_change_blurs_and_preserves_session_without_start(tmp_path):
    a=app(tmp_path); s=a.terminal_state
    identity=T.input_identity(a)
    a.tab='history'; T.begin_frame(a)
    assert not T.focused(a) and s.active=='one' and len(s.service.sessions)==1
    assert s.focus_generation>identity[1]
    a.tab='jobs'; T.begin_frame(a)
    assert T.showing(a) and not T.focused(a)


def test_render_feedback_preserves_neighbor_and_cached_rows(tmp_path):
    a=app(tmp_path); s=a.terminal_state
    rows=[[('L'*55+'-'*40+'R'*5,'text')] for _ in range(30)]
    result=T.feedback(a,rows)
    assert result is not rows
    assert L.row_text(rows[6])=='L'*55+'-'*40+'R'*5
    for index in range(6,18):
        text=L.row_text(result[index])
        assert text.startswith('L'*55) and text.endswith('R'*5)
    assert result[5]==rows[5] and result[18]==rows[18]
    first=list(s.service.resizes)
    T.feedback(a,rows)
    assert s.service.resizes==first


@pytest.mark.parametrize('width,height',[(0,0),(1,1),(2,2),(8,3),(20,5),(80,24)])
def test_tiny_panes_fit_and_controls_never_escape(tmp_path,width,height):
    a=app(tmp_path)
    rows,hits=T.render(a,width,height,x=3,y=4)
    assert len(rows)==height
    assert all(L.vlen(L.row_text(row))<=width for row in rows)
    assert all(0<=y<height and 0<=value['left']<value['right']<=width for y,_,value in hits)
    assert all(3<=left<right<=3+width for _,left,right,_ in a.terminal_state.controls)


def test_selection_copy_contains_only_pinned_terminal_rows(tmp_path,monkeypatch):
    a=app(tmp_path); s=a.terminal_state
    s.service.sessions['one']['lines']=[[(f'line {i}','text')] for i in range(8)]
    T.run_command(a,['terminal','copy-mode']); T.render(a,40,12,x=55,y=6)
    r=s.body_rect
    T.handle_mouse(a,r.y+1,r.x+2,'left'); T.handle_mouse(a,r.y+2,r.x+2,'drag'); T.handle_mouse(a,r.y+2,r.x+2,'release')
    assert s.selected==('line 1','line 2')
    s.service.sessions['one']['lines']=[[('replacement','text')]]
    copied=[]
    monkeypatch.setattr('tower.clipboard.copy',lambda text,*args,**kwargs:copied.append(text) or 'copied')
    assert T.handle_key(a,'y')
    assert copied==['line 1\nline 2']
    T.handle_mouse(a,r.y,r.x,'right'); T.handle_key(a,'y')
    assert copied==['line 1\nline 2']
    assert 'Select terminal lines' in a.messages[-1]


def test_keyboard_copy_selects_and_extends_without_mouse(tmp_path):
    a=app(tmp_path); s=a.terminal_state
    T.run_command(a,['terminal','copy-mode'])
    T.handle_key(a,'v'); T.handle_key(a,'down')
    assert s.selected==('one','two')


def test_preview_never_executes_and_is_bound_to_original_session(tmp_path):
    a=app(tmp_path); s=a.terminal_state
    s.service.events.clear()
    assert T.prepare_input(a,'echo safe','Review command')
    assert s.service.events==[]
    T.run_command(a,['terminal','insert'])
    assert s.service.events==[('one',{'type':'paste','text':'echo safe'})]
    T.prepare_input(a,'echo stale')
    s.active='different'; T.run_command(a,['terminal','insert'])
    assert len(s.service.events)==1


def test_inactive_terminal_never_handles_keys_or_paste(tmp_path):
    a=app(tmp_path); s=a.terminal_state
    T.run_command(a,['terminal','details'])
    count=len(s.service.events)
    assert not T.handle_key(a,'q') and not T.handle_event(a,{'type':'paste','text':'x'})
    assert len(s.service.events)==count


def test_configured_paste_limit_is_enforced_before_service(tmp_path):
    a=app(tmp_path); s=a.terminal_state; a.cfg['terminal']={'paste_limit_bytes':4}
    before=len(s.service.events)
    T.handle_event(a,{'type':'paste','text':'12345'})
    assert len(s.service.events)==before and 'limit' in a.messages[-1]


def test_session_switch_preserves_independent_shell_but_clears_selection(tmp_path):
    a=app(tmp_path); s=a.terminal_state
    s.service.sessions['two']={**s.service.sessions['one'],'id':'two','label':'second'}
    s.selected=('old selection',)
    old=T.input_identity(a)
    T.run_command(a,['terminal','next'])
    assert s.active=='two' and s.selected is None and T.input_identity(a)!=old
    T.run_command(a,['terminal','previous'])
    assert s.active=='one'


def test_health_report_is_local_and_never_sent_as_shell_input(tmp_path):
    a=app(tmp_path); s=a.terminal_state; s.service.events.clear()
    T.run_command(a,['terminal','health']); T.handle_key(a,'q')
    assert s.report is None and not s.service.events


def test_layout_terminal_bypasses_details_reflow_and_keeps_scroll(tmp_path):
    a=app(tmp_path); s=a.terminal_state; a.width=160
    W.initialize(a).scroll['jobs:details']=27
    groups={'main':{'rows':[[('Job1','text')]],'hits':[(0,'job','1')]},
            'details':{'rows':[[('old details','text')]],'hits':[]}}
    rows,hits=W.transform_body(a,[],[],160,20,groups=groups)
    assert W.initialize(a).scroll['jobs:details']==27
    assert 'old details' not in L.to_text(rows,160)
    assert any(k=='control' and v['id']=='terminal:show' for _,k,v in hits)


@pytest.mark.parametrize('tab',['analytics','deps','log','research'])
def test_shared_workspace_terminal_isolated_and_details_restored(tmp_path,tab):
    a=app(tmp_path,tab=tab); a.width=160
    def renderer(width,height):
        return [[('native '+str(i),'text')] for i in range(height)],[]
    T.begin_frame(a)
    rows,hits=T.wrap_workspace(a,160,20,renderer)
    assert any('native' in L.row_text(row) for row in rows)
    assert any(k=='control' and v['id']=='terminal:show' for _,k,v in hits)
    assert a.terminal_state.rect.x>0
    T.run_command(a,['terminal','details']); T.begin_frame(a)
    rows,hits=T.wrap_workspace(a,160,20,renderer)
    assert 'native 0' in L.row_text(rows[0])
    assert a.terminal_state.service is not None


def test_shutdown_only_owned_service(tmp_path):
    a=app(tmp_path); service=a.terminal_state.service
    T.shutdown(a); T.shutdown(a)
    assert service.shutdowns==1 and not T.focused(a)


def test_pending_prefix_before_paste_is_literal_and_cannot_steal_next_t(tmp_path):
    a=app(tmp_path); s=a.terminal_state; s.service.events.clear()
    T.handle_key(a,'ctrl-]'); T.handle_event(a,{'type':'paste','text':'abc'}); T.handle_key(a,'t')
    assert [event for _,event in s.service.events]==[
        {'type':'key','key':'ctrl-]'},{'type':'paste','text':'abc'},{'type':'key','key':'t'}]
    assert T.focused(a)


def test_missing_rendered_pane_releases_keyboard_owner(tmp_path):
    a=app(tmp_path)
    T.begin_frame(a); T.finalize_frame(a)
    assert not T.focused(a) and a.terminal_state.body_rect is None


def test_resize_to_zero_body_releases_keyboard_owner(tmp_path):
    a=app(tmp_path)
    T.render(a,80,3)
    assert not T.focused(a)


def test_report_end_shows_last_line(tmp_path):
    a=app(tmp_path); s=a.terminal_state
    T.show_report(a,[f'report {n}' for n in range(80)])
    T.render(a,40,12,x=55,y=6)
    T.handle_key(a,'end')
    rows,_=T.render(a,40,12,x=55,y=6)
    assert 'report 79' in L.to_text(rows,40)


def test_all_history_pages_use_one_coherent_snapshot_token(tmp_path):
    a=app(tmp_path); s=a.terminal_state; service=s.service
    snapshot=service.sessions['one']; snapshot.update(history_count=2100,history_start=100)
    T.request_history(a)
    assert service.requests[-1]==('one','history',{'offset':0,'limit':1000})
    snapshot['result']={'action':'history','request_id':10,'result':{
        'lines':[f'old {i}' for i in range(1000)],'offset':0,'total':2100,'history_start':100,'snapshot_id':'frozen'}}
    T.tick(a)
    history_requests=[request for request in service.requests if request[1]=='history']
    assert history_requests[1:]==[
        ('one','history',{'offset':1000,'limit':1000,'snapshot_id':'frozen'}),
        ('one','history',{'offset':2000,'limit':100,'snapshot_id':'frozen'})]
    snapshot['history_start']=5000
    snapshot['results']=(
        {'action':'history','request_id':11,'result':{'lines':[f'old {i}' for i in range(1000,2000)],'offset':1000,'total':2100,'history_start':100,'snapshot_id':'frozen'}},
        {'action':'history','request_id':12,'result':{'lines':[f'old {i}' for i in range(2000,2100)],'offset':2000,'total':2100,'history_start':100,'snapshot_id':'frozen'}})
    T.tick(a)
    assert len(s.history['one'])==2100 and s.history['one'][-1]=='old 2099'
    assert not s.history_loading['one']


def test_ascii_chrome_is_ascii_but_child_unicode_is_preserved(tmp_path):
    a=app(tmp_path); a.ascii=True; a.animations_enabled=False
    a.terminal_state.service.sessions['one']['lines']=[[('λ','text')]]
    rows,_=T.render(a,50,12)
    assert all(L.row_text(rows[index]).isascii() for index in (0,1,2,11))
    assert 'λ' in L.to_text(rows,50)


def test_resize_during_capture_drops_capture_and_consumes_release(tmp_path):
    a=app(tmp_path); s=a.terminal_state; r=s.body_rect
    T.handle_mouse(a,r.y,r.x,'left')
    T.render(a,35,10,x=60,y=7)
    assert s.capture is None and s.discard_release
    assert T.handle_mouse(a,0,0,'release')


def test_idle_feedback_reuses_terminal_render(tmp_path,monkeypatch):
    a=app(tmp_path); a.animations_enabled=False
    rows=[[(' '*100,'')] for _ in range(30)]
    T.render(a,40,12,x=55,y=6)
    monkeypatch.setattr(T,'_render',lambda *args:pytest.fail('idle pane rebuilt'))
    T.feedback(a,rows); T.feedback(a,rows)


def test_copy_joins_real_soft_wraps_and_preserves_hard_newline(tmp_path,monkeypatch):
    a=app(tmp_path); s=a.terminal_state
    s.service.sessions['one']['lines']=[[('soft','text')],[('wrap','text')],[('hard','text')]]
    s.service.sessions['one']['line_wrapped']=[True,False,False]
    T.run_command(a,['terminal','copy-mode'])
    T.handle_key(a,'v'); T.handle_key(a,'down'); T.handle_key(a,'down')
    copied=[]
    monkeypatch.setattr('tower.clipboard.copy',lambda text,*a,**kw:copied.append(text) or 'copied')
    T.handle_key(a,'y')
    assert copied==['softwrap\nhard']


def test_child_clipboard_is_previewed_and_never_copied_on_output(tmp_path,monkeypatch):
    a=app(tmp_path); s=a.terminal_state; copied=[]
    monkeypatch.setattr('tower.clipboard.copy',lambda text,*a,**kw:copied.append(text) or 'copied')
    s.service.sessions['one']['clipboard_requests']=[{'id':1,'text':'requested text','selection':'c'}]
    T.tick(a); T.render(a,50,14)
    assert not copied
    T.run_command(a,['terminal','child-clipboard','1'])
    assert not copied and s.clipboard_preview['text']=='requested text'
    T.run_command(a,['terminal','accept-clipboard'])
    assert copied==['requested text']


def test_link_destination_is_explicit_and_health_cannot_copy_stale_link(tmp_path,monkeypatch):
    a=app(tmp_path); s=a.terminal_state; copied=[]
    monkeypatch.setattr('tower.clipboard.copy',lambda text,*a,**kw:copied.append(text) or 'copied')
    s.service.sessions['one']['links']=[{'id':1,'url':'https://example.test/safe'}]
    T.run_command(a,['terminal','links','1'])
    assert s.clipboard_preview['text']=='https://example.test/safe'
    T.run_command(a,['terminal','health'])
    assert s.clipboard_preview is None
    T.run_command(a,['terminal','accept-clipboard'])
    assert not copied


def test_scrollback_rail_drag_has_own_bounded_capture(tmp_path):
    a=app(tmp_path); s=a.terminal_state
    s.service.sessions['one']['lines']=[[(str(n),'text')] for n in range(100)]
    T.render(a,40,12,x=55,y=6)
    rail=s.rail_rect
    assert rail.x==94 and s.body_rect.width==39
    assert T.handle_mouse(a,rail.y,rail.x,'left')
    assert s.capture[-1]=='rail'
    assert T.handle_mouse(a,rail.y+100,rail.x,'drag')
    assert s.top==100-rail.height and not s.follow
    assert T.handle_mouse(a,0,0,'release') and s.capture is None


def test_multiline_paste_preview_retains_exact_payload_and_requires_insert(tmp_path):
    a=app(tmp_path); s=a.terminal_state; s.service.events.clear()
    payload='echo first\necho second\n'
    assert T.handle_event(a,{'type':'paste','text':payload})
    assert not s.service.events and s.preview['text']==payload
    T.run_command(a,['terminal','insert'])
    assert s.service.events==[('one',{'type':'paste','text':payload})]


def test_multiline_paste_preview_can_be_disabled(tmp_path):
    a=app(tmp_path); s=a.terminal_state; a.cfg['terminal']={'paste_preview':False}
    T.handle_event(a,{'type':'paste','text':'echo direct\n'})
    assert s.preview is None and s.service.events[-1][1]['text']=='echo direct\n'


def test_report_reflows_long_commands_and_end_reaches_last_fragment(tmp_path):
    a=app(tmp_path)
    T.show_report(a,['prefix '+('abcdef '*50)+'THE_END'])
    T.render(a,25,12,x=55,y=6)
    T.handle_key(a,'end')
    rows,_=T.render(a,25,12,x=55,y=6)
    visible=''.join(L.row_text(row)[:-1] for row in rows[4:-1]).rstrip()
    assert visible.endswith('THE_END')
    assert T._report_document(a)['total']>10


def test_report_byte_budget_is_bounded_and_visible(tmp_path):
    a=app(tmp_path)
    T.show_report(a,('x'*4096 for _ in range(1000)))
    assert sum(len(line.encode()) for line in a.terminal_state.report['lines'])<T.MAX_SELECTION_BYTES+200
    assert 'truncated' in a.terminal_state.report['lines'][-1]


def test_tools_job_directory_prompt_opens_declared_job_directory(tmp_path,monkeypatch):
    import threading
    a=app(tmp_path)
    directory=tmp_path/'declared working directory'
    directory.mkdir()
    a.store=SimpleNamespace(lock=threading.RLock(),jobs=[SimpleNamespace(id='42',workdir=str(directory))],finished=[])
    a.files=SimpleNamespace(remote=False)
    a.cfg['terminal']={'shell':'/bin/bash'}
    opened=[]
    monkeypatch.setattr(T,'open_session',lambda owner,**options:opened.append(options) or 'new-session')
    command=next(args for label,args in T._actions() if label=='Job directory')
    assert T.run_command(a,command)
    assert a.terminal_state.dialog['action']=='job-cwd'
    for key in '42':
        assert T.handle_key(a,key)
    assert T.handle_key(a,'enter')
    assert len(opened)==1
    assert opened[0]['cwd']==str(directory)
    assert opened[0]['label']=='job-directory:42'
    assert opened[0]['kind']=='local'
    assert a.terminal_state.service.events[-1][1].get('type') != 'paste'


@pytest.mark.parametrize('kind,argv',[('allocation-shell',['srun','--jobid=42','/bin/bash']),
                                    ('step-attach',['sattach','42.0']),
                                    ('local',['/usr/bin/srun','--jobid=42','/bin/bash'])])
def test_restart_cannot_reuse_reviewed_allocation_launch(tmp_path,monkeypatch,kind,argv):
    a=app(tmp_path); s=a.terminal_state
    s.launches['one']={'kind':kind,'argv':argv,'metadata':{'destination':'allocation'}}
    monkeypatch.setattr(T,'open_session',lambda *a,**kw:pytest.fail('review was bypassed'))
    assert T.run_command(a,['terminal','restart'])
    assert s.active=='one' and 'one' in s.service.sessions
    assert s.launches['one']['argv']==argv
    assert 'fresh review' in a.messages[-1]
