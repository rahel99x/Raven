"""Reviewed allocation controls, shared hit geometry, and passive publications."""
from types import SimpleNamespace
import copy

import pytest

from raven import layout as L, raven_ui as R, terminal_ui as T


class Manager:
    def __init__(self):
        self.value={'state':'light','mode':'light','generation':0,'request_id':None,
                    'target':'slurm','job_id':None,'host':None,'cleanup_uncertain':False}
        self.calls=[]
        self.reads=0
        self.digest='profile-1'
    def snapshot(self):
        self.reads+=1
        return copy.deepcopy(self.value)
    def tick(self):
        return False
    def plan(self):
        self.calls.append(('plan',))
        return {'digest':self.digest,'target':'slurm','site':'cluster-a',
                'resources':{'account':'research','partition':'compute','cpus_per_task':2,'memory_mib':4096}}
    def request(self,*,plan):
        if plan['digest']!=self.digest:
            raise ValueError('profile changed')
        self.calls.append(('request',copy.deepcopy(plan)))
        self.value.update(state='pending',generation=self.value['generation']+1,request_id='owned-request')
        return 'owned-request'
    def cancel(self,*,generation):
        self.calls.append(('cancel',generation))
        self.value.update(state='draining')
    def release(self,*,generation):
        self.calls.append(('release',generation))
        self.value.update(state='draining')
    def retry_cleanup(self, *, generation):
        self.calls.append(('cleanup', generation))
        return True
    def shutdown(self):
        self.calls.append(('shutdown',))


def app():
    a=SimpleNamespace(mode='main',tab='jobs',width=100,height=30,selected_id='scientific-42',
                      marks={'scientific-41','scientific-42'},messages=[],cfg={'raven':{
                          'execution':{'heavy_target':'slurm'}}})
    a.say=lambda text:a.messages.append(text)
    a.fail=lambda text:a.messages.append('ERROR '+text)
    R.initialize(a)
    R.bind(a,Manager())
    from raven.workbench import bind_actions
    bind_actions(a)
    return a


def render(a,ascii_=False):
    return R.overlay(SimpleNamespace(g=L.Glyphs(ascii_)),{},a,a.width,a.height)


def click(a,key,button='left'):
    y,_,item=next(hit for hit in a.raven_ui_state['control_hits'] if hit[2]['id']=='raven:'+key)
    return R.handle_mouse(a,y,item['left'],button)


def test_initialization_binding_and_paint_never_request_compute():
    a=app(); m=a.raven_allocation_manager
    assert not m.calls
    R.run_command(a,['raven','status'])
    before=m.reads
    render(a); R.button(a,23); R.headline(a); R.summary(a)
    assert m.reads==before and not m.calls


def test_request_requires_review_and_same_mouse_keyboard_action():
    a=app(); m=a.raven_allocation_manager
    R.run_command(a,['raven','status']); render(a)
    click(a,'request')
    assert m.calls==[('plan',)]
    R.handle_key(a,'enter')  # The default review choice is Back, never Confirm.
    assert not a.raven_ui_state['review'] and m.calls==[('plan',)]
    R.run_command(a,['raven','request']); render(a)
    click(a,'accept','motion'); click(a,'accept','release')
    assert len(m.calls)==2
    click(a,'accept','press')
    assert len(m.calls)==3 and m.calls[-1][0]=='request'
    R.run_command(a,['raven','accept'])
    assert len(m.calls)==3
    assert a.selected_id=='scientific-42' and len(a.marks)==2


@pytest.mark.parametrize('change',['generation','manager','tab','mode'])
def test_stale_review_cannot_target_another_request_or_page(change):
    a=app(); m=a.raven_allocation_manager
    R.run_command(a,['raven','request'])
    if change=='generation': m.value['generation']=4
    if change=='manager': a.raven_allocation_manager=Manager()
    if change=='tab': a.tab='research'
    if change=='mode': a.mode='main'
    R.run_command(a,['raven','accept'])
    assert not any(call[0]=='request' for call in m.calls)
    assert 'review' in a.messages[-1].lower()


def test_changed_policy_refuses_frozen_request_plan():
    a=app(); m=a.raven_allocation_manager
    R.run_command(a,['raven','request']); m.digest='new-policy'
    R.run_command(a,['raven','accept'])
    assert not any(call[0]=='request' for call in m.calls)
    assert 'profile changed' in a.messages[-1]


def test_grant_racing_cancel_keeps_originating_generation():
    a=app(); m=a.raven_allocation_manager
    m.value.update(state='pending',generation=7,request_id='same-request')
    R.tick(a); R.run_command(a,['raven','cancel'])
    m.value.update(state='heavy',job_id='103',host='compute-3')
    R.tick(a); R.run_command(a,['raven','accept'])
    assert m.calls==[('cancel',7)]
    assert a.selected_id=='scientific-42'


def test_release_review_targets_owned_manager_not_selected_job():
    a=app(); m=a.raven_allocation_manager
    m.value.update(state='heavy',generation=8,request_id='own',job_id='103',host='compute-3')
    R.tick(a); R.run_command(a,['raven','release'])
    R.handle_key(a,'right'); R.handle_key(a,'enter')
    assert m.calls==[('release',8)]


def test_background_transition_keeps_terminal_focus_and_pending_gesture():
    a=app(); state=T.initialize(a)
    state.active='pty-1'; state.modes['jobs']='terminal'; state.keyboard_focus=True
    state.capture=('pty-1',state.focus_generation,'child')
    identity=T.input_identity(a)
    a.raven_allocation_manager.value.update(state='heavy',generation=1,job_id='103',host='compute-3')
    assert R.tick(a)
    assert a.mode=='main' and T.input_identity(a)==identity
    assert state.capture==('pty-1',state.focus_generation,'child')
    assert a.selected_id=='scientific-42' and len(a.marks)==2


@pytest.mark.parametrize('change',['resize','generation','state'])
def test_stale_mouse_rectangles_cannot_start_action(change):
    a=app(); m=a.raven_allocation_manager
    R.run_command(a,['raven','status']); render(a)
    y,_,item=next(hit for hit in a.raven_ui_state['control_hits'] if hit[2]['id']=='raven:request')
    if change=='resize': a.width-=10
    if change=='generation': m.value['generation']+=1; R.tick(a)
    if change=='state': m.value['state']='reconciling'; R.tick(a)
    R.handle_mouse(a,y,item['left'],'press')
    assert not m.calls


def test_countdown_update_preserves_current_action_geometry():
    a=app(); m=a.raven_allocation_manager
    m.value.update(state='heavy',generation=3,request_id='own',remaining_seconds=120)
    R.tick(a); R.run_command(a,['raven','status']); render(a)
    m.value['remaining_seconds']=119; R.tick(a)
    assert a.raven_ui_state['control_hits']
    click(a,'release')
    assert a.raven_ui_state['review']['action']=='release'


@pytest.mark.parametrize('width,height',[(1,1),(20,8),(40,12),(100,30)])
@pytest.mark.parametrize('ascii_',[False,True])
def test_compute_modal_controls_stay_within_drawn_bounds(width,height,ascii_):
    a=app(); a.width=width; a.height=height
    R.run_command(a,['raven','status'])
    for unused in range(4):
        rows=render(a,ascii_)
        assert all(0<=y<height and 0<=x<width and x+L.vlen(L.row_text(row))<=width for y,x,row in rows)
        assert all(0<=y<height and 0<=item['left']<item['right']<=width for y,_,item in a.raven_ui_state['control_hits'])
        if ascii_: assert all(L.row_text(row).isascii() for _,_,row in rows)
        R.handle_key(a,'right')


def test_uncertain_cleanup_is_light_and_blocks_retry():
    a=app(); m=a.raven_allocation_manager
    m.value.update(state='heavy',cleanup_uncertain=True,error='lost connection',job_id='103')
    R.tick(a)
    assert R.status(a)['mode']=='light' and 'uncertain' in R.summary(a)
    assert [key for key,label in R._choices(a)]==['inspect','cleanup','close']
    R.run_command(a,['raven','request'])
    assert not m.calls and 'changed' in a.messages[-1]
    assert any('Light remains available' in line for line in R._lines(a))
    R.run_command(a,['raven','cleanup'])
    assert not m.calls  # Reviewing cleanup never launches or cancels immediately.
    R.run_command(a,['raven','accept'])
    assert m.calls == [('cleanup', 0)]
    assert R.status(a)['cleanup_uncertain']  # Only verified reconciliation clears it.


def test_plain_reuse_jobid_never_attaches_to_scientific_job():
    a=app()
    R.run_command(a,['raven','reuse','42'])
    assert not a.raven_allocation_manager.calls
    assert 'verified ownership' in a.messages[-1]


@pytest.mark.parametrize('job_id',['123_4','123+4','-1','1;echo',''])
def test_reuse_requires_allocation_id_not_task_or_command(job_id):
    a=app()
    a.raven_allocation_manager.request_reuse_review=lambda jid:pytest.fail('invalid verification')
    R.run_command(a,['raven','reuse',job_id])
    assert 'numeric Slurm allocation JobID' in a.messages[-1]


def test_real_manager_plan_review_paints_charging_and_ownership_without_process(tmp_path):
    from raven.raven_allocation import AllocationManager
    config={'site':{'kind':'slurm','cluster_id':'test-cluster'},'execution':{'heavy_target':'slurm'},
        'connection':{'transport':'local'},'allocation':{'account':'research','partition':'compute',
        'nodes':1,'tasks':1,'cpus_per_task':2,'memory_mib':512,'walltime_seconds':30},
        'resources':{'heavy':{'memory_mib':512,'max_workers':2,'max_tasks':1}}}
    with AllocationManager(config,state_dir=tmp_path) as manager:
        a=app(); R.bind(a,manager)
        R.run_command(a,['raven','request'])
        lines='\n'.join(R._lines(a))
        assert 'research' in lines and 'compute' in lines and 'dedicated-allocation' in lines
        assert manager._process is None
        render(a)
        assert a.raven_ui_state['review']['plan']['digest']==manager.plan()['digest']


def test_reuse_verification_is_async_and_confirm_requires_matching_receipt(monkeypatch):
    a=app(); m=a.raven_allocation_manager
    def verify(jid):
        m.value['reuse_review']={'state':'reviewing','job_id':jid,'generation':0,'token':'new-review'}
        return 'new-review'
    m.request_reuse_review=verify
    R.run_command(a,['raven','reuse'])
    for key in '103': R.handle_key(a,key)
    R.handle_key(a,'enter')
    assert a.raven_ui_state['reuse_pending'] and not a.raven_ui_state['review']
    plan=m.plan(); m.calls.clear()
    m.value['reuse_review'].update(state='ready',plan=plan,expires_at=10**12,token='stale-review')
    R.tick(a)
    assert not a.raven_ui_state['review']
    m.value['reuse_review']['token']='new-review'
    R.tick(a)
    assert a.raven_ui_state['review']['reuse_token']=='new-review'
    accepted=[]
    monkeypatch.setattr(m,'request',lambda **kwargs:accepted.append(kwargs))
    R.handle_key(a,'right'); R.handle_key(a,'enter')
    assert accepted==[{'reuse':'new-review','plan':plan}]
    assert a.selected_id=='scientific-42'


def test_expired_reuse_receipt_refreshes_without_starting_worker(monkeypatch):
    a=app(); m=a.raven_allocation_manager
    reviews=[]
    def verify(jid):
        reviews.append(jid)
        m.value['reuse_review']={'state':'reviewing','job_id':jid,'generation':0,'token':str(len(reviews))}
        return str(len(reviews))
    m.request_reuse_review=verify
    R.run_command(a,['raven','reuse','103'])
    m.value['reuse_review'].update(state='ready',plan=m.plan(),expires_at=1)
    R.tick(a); R.run_command(a,['raven','accept'])
    assert reviews==['103','103'] and a.raven_ui_state['reuse_pending']
    assert not any(call[0]=='request' for call in m.calls)


def test_reuse_result_never_reopens_closed_modal():
    a=app(); m=a.raven_allocation_manager
    m.request_reuse_review=lambda jid:'review'
    R.run_command(a,['raven','reuse','103']); R.run_command(a,['raven','close'])
    m.value['reuse_review']={'state':'ready','job_id':'103','generation':0,'token':'review','plan':m.plan()}
    R.tick(a)
    assert a.mode=='main' and not a.raven_ui_state['review']


def test_modal_buttons_publish_graph_actions():
    from raven import interaction
    a=app(); R.run_command(a,['raven','status'])
    overlays=render(a)
    interaction.publish(a,[[(' '*a.width,'')]]*a.height,[],a.width,a.height,overlays=overlays)
    controls=interaction.initialize(a)['graph'].controls
    assert any(control.id=='raven:request' and control.action==('command','raven request') for control in controls)


def test_palette_and_toolbar_share_allocation_commands():
    from raven.command_registry import toolbar_controls
    controls=toolbar_controls('View')
    for action in ('status','request','inspect','cancel','release'):
        assert any(item.command=='raven '+action for item in controls)


def test_unobserved_empty_queue_differs_from_observed_empty_queue(monkeypatch):
    from raven.model import Health
    from raven import clock
    a=app(); monkeypatch.setattr(clock,'now',lambda:1000)
    snap={'jobs':[],'finished':[],'health':{'jobs':Health('jobs'),'finished':Health('finished')}}
    assert 'Waiting for governed queue/history observation' in R.observation_notice(a,snap)
    snap['health']['jobs'].last_ok=997
    assert R.observation_notice(a,snap)=='Waiting for governed history observation; queue age 3s.'
    snap['health']['finished'].last_ok=998
    assert R.observation_notice(a,snap) is None
    assert snap['jobs']==[] and snap['finished']==[]


def test_disabled_desktop_slurm_is_unavailable_not_waiting():
    a=app(); a.cfg['raven'].update(site={'kind':'desktop'},collection={'slurm_enabled':False})
    notice=R.observation_notice(a,{'jobs':[],'finished':[],'health':{}})
    assert notice=='Slurm unavailable: collection is disabled by this desktop profile.'
    assert 'Waiting' not in notice


def test_disabled_source_and_legacy_observation_notices():
    from raven.model import Health
    a=app()
    notice=R.observation_notice(a,{'health':{'jobs':Health('jobs',enabled=False),'finished':Health('finished',last_ok=1)}})
    assert 'Queue unavailable: source disabled' in notice and 'Waiting' not in notice
    a.cfg={}
    assert R.observation_notice(a,{'health':{}}) is None


@pytest.mark.parametrize('width',[40,80,140])
@pytest.mark.parametrize('tab',['jobs','history'])
def test_observation_banner_reuses_header_row_without_moving_tab_hits(width,tab):
    from raven.config import Config
    from raven.controller import App
    from raven.model import Health,Store
    from raven.views import Views
    cfg=Config(); cfg.data['raven']={'site':{'kind':'slurm'},'collection':{'slurm_enabled':True}}
    store=Store(persist=False)
    a=App(store,None,None,cfg,'researcher',ascii_=True)
    a.tab=tab
    views=Views(L.Glyphs(True),cfg)
    snap=store.snapshot(); snap['health']={'jobs':Health('jobs'),'finished':Health('finished')}
    waiting=views.header(snap,a,width); hits=list(a.tab_hits)
    assert 'Waiting for governed' in L.row_text(waiting[2])
    assert L.vlen(L.row_text(waiting[2]))<=width
    snap['health']['jobs'].last_ok=snap['health']['finished'].last_ok=1
    observed=views.header(snap,a,width)
    assert len(observed)==len(waiting) and a.tab_hits==hits
    assert 'Waiting for governed' not in L.row_text(observed[2])
