"""Quiet managed Jobs retain documents without delaying publication or input."""
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from tower import clock,layout as L,screen
from tower.config import Config
from tower.controller import App
from tower.model import Health,Job,Store
from tower.raven_limits import LightLimits
from tower.views import Views


@pytest.fixture
def scene(monkeypatch):
    now=[1000.]
    monkeypatch.setattr(screen.time,'monotonic',lambda:now[0])
    monkeypatch.setattr(screen.time,'time',lambda:now[0])
    monkeypatch.setattr(clock,'now',lambda:now[0])
    cfg=Config({'animations':True,'startup_animation':False,'log_lines':0,
                'raven':{'managed':True,'collection':{'slurm_enabled':True}}})
    store=Store(persist=False,raven_limits=LightLimits({}))
    store.apply_jobs([Job('7','quiet','cpu','RUNNING',cpus=2)])
    store.health={'jobs':Health('jobs',calls=1,last_ok=900),'finished':Health('finished',calls=1,last_ok=900)}
    app=App(store,None,None,cfg,'tester',interactive=True)
    views=Views(L.Glyphs(False),cfg); app.views_ref=views
    cache=screen._FrameCache(); ticks=[]
    original=app.tick
    def tick():
        ticks.append(now[0]); original()
    monkeypatch.setattr(app,'tick',tick)
    cache.rebuild(app,views,store,None,160,40)
    result=SimpleNamespace(app=app,views=views,store=store,cache=cache,now=now,ticks=ticks)
    yield result
    if app.research: app.research.close()


def maintenance(scene,elapsed=.201):
    scene.now[0]+=elapsed
    return scene.cache.maintain(scene.app,160,40)


def test_four_housekeeping_passes_reuse_document_then_second_boundary_refreshes(scene,monkeypatch):
    s=scene
    assert s.cache.document_token is not None
    rows,hits,graph=s.cache.rows,s.cache.hits,s.app.interaction_state['graph']
    def forbidden(*args,**kwargs): pytest.fail('unchanged document composed or snapshotted')
    monkeypatch.setattr(s.store,'snapshot',forbidden)
    monkeypatch.setattr(s.views,'compose',forbidden)
    for unused in range(4):
        assert maintenance(s)
        assert not s.cache.due(s.app,160,40)
        s.cache.feedback(s.app,s.views)
    assert len(s.ticks)==5 and s.cache.rows is rows and s.cache.hits is hits
    assert s.app.interaction_state['graph'] is graph
    assert maintenance(s)
    assert s.cache.due(s.app,160,40)
    assert len(s.ticks)==6


@pytest.mark.parametrize('publication',['queue','detail','error','event','metric','replacement'])
def test_source_publications_force_current_rows_without_waiting_for_second(scene,publication):
    s=scene
    if publication=='queue': s.store.apply_jobs([Job('8','new job','cpu','RUNNING')])
    elif publication=='detail':
        s.store.details['7']={'Reason':'new reason'}
        s.store.health['jobs'].calls+=1
    elif publication=='error': s.store.health['jobs'].error='Accounting unavailable'
    elif publication=='event': s.store.event('notice','background event')
    elif publication=='metric': s.store.record('7',{'t':1000,'cpu':.5})
    else: s.store.jobs=[Job('9','direct replacement','cpu','RUNNING')]
    assert maintenance(s)
    assert s.cache.dirty and s.cache.due(s.app,160,40)
    s.cache.rebuild(s.app,s.views,s.store,None,160,40,maintained=True)
    if publication in ('queue','replacement'):
        expected='8' if publication=='queue' else '9'
        if publication=='replacement': assert s.app.selected_id==expected
        assert any(kind=='job' and value==expected for _,kind,value in s.cache.hits)
    assert len(s.ticks)==2  # Housekeeping is not repeated by the same rebuild.


def test_snapshot_publication_token_does_not_skip_change_during_compose(scene,monkeypatch):
    s=scene; compose=s.views.compose
    def changed(*args,**kwargs):
        result=compose(*args,**kwargs)
        s.store.health['jobs'].calls+=1
        return result
    monkeypatch.setattr(s.views,'compose',changed)
    s.cache.rebuild(s.app,s.views,s.store,None,160,40)
    assert maintenance(s) and s.cache.dirty


def test_captured_unknown_publication_cannot_be_replaced_with_newer_live_token(scene,monkeypatch):
    s=scene; compose=s.views.compose
    # The unknown producer shape intentionally disables the quiet token, but
    # remains renderable as source health. Composition replaces it with a
    # native publication only after the old snapshot has been painted.
    class ExtensionHealth(Health):
        pass
    s.store.health['jobs']=ExtensionHealth(**vars(s.store.health['jobs']))
    assert s.store.ui_publication_token() is None
    def changed(*args,**kwargs):
        result=compose(*args,**kwargs)
        s.store.health['jobs']=Health('jobs',calls=2,last_ok=1000)
        return result
    monkeypatch.setattr(s.views,'compose',changed)
    s.cache.rebuild(s.app,s.views,s.store,None,160,40)
    assert s.store.ui_publication_token() is not None
    assert s.cache.document_token is None
    assert not maintenance(s) and s.cache.due(s.app,160,40)


def test_hub_callback_without_message_invalidates_document(scene):
    from tower.research import ResearchHub
    s=scene; hub=s.app.research=ResearchHub(s.app.cfg,s.views.files)
    s.cache.rebuild(s.app,s.views,s.store,None,160,40)
    future=Future(); future.set_result('published')
    hub.future=future
    def complete(value): s.app.job_panel_state['callback_result']=value
    hub.pending=(future,complete)
    previous=s.app.ui_publication_revision
    assert not maintenance(s)  # Pending callbacks use the original cadence.
    assert s.cache.due(s.app,160,40)
    s.cache.rebuild(s.app,s.views,s.store,None,160,40)
    assert s.app.ui_publication_revision==previous+1
    assert s.app.job_panel_state['callback_result']=='published'


def test_message_expiry_and_manager_publication_invalidate_without_source_call(scene):
    s=scene
    s.app.message='finished'; s.app.message_t=s.now[0]-5.9
    s.cache.rebuild(s.app,s.views,s.store,None,160,40)
    assert maintenance(s) and s.cache.dirty and not s.app.message
    s.cache.rebuild(s.app,s.views,s.store,None,160,40,maintained=True)
    s.app.raven_ui_state['revision']+=1
    assert maintenance(s) and s.cache.dirty


@pytest.mark.parametrize('reason',['graph','completion','scrolling','startup','other-pane','other-page','terminal','unknown-store','unmanaged'])
def test_unknown_or_animating_views_keep_original_document_deadline(scene,monkeypatch,reason):
    s=scene
    if reason=='graph': monkeypatch.setattr('tower.metric_live.document_interval',lambda app:.1)
    elif reason=='completion': s.app.completion=SimpleNamespace(active=True)
    elif reason=='scrolling': monkeypatch.setattr('tower.scrolling.active',lambda app:True)
    elif reason=='startup': monkeypatch.setattr('tower.startup.active',lambda app:True)
    elif reason=='other-pane': s.app.job_panel_state['mode']='analytics'
    elif reason=='other-page': s.app.tab='history'
    elif reason=='terminal': monkeypatch.setattr('tower.terminal_ui.showing',lambda app:True)
    elif reason=='unknown-store': s.app.store=SimpleNamespace()
    else: s.app.cfg.data.pop('raven')
    assert not maintenance(s)
    assert s.cache.due(s.app,160,40)


def test_direct_mutation_in_unmanaged_fixture_keeps_original_fallback(scene):
    s=scene; s.app.cfg.data.pop('raven')
    s.store.jobs[0].name='direct mutation'
    assert not maintenance(s) and s.cache.due(s.app,160,40)


def test_input_geometry_and_feedback_animation_deadlines_are_not_delayed(scene):
    s=scene
    s.cache.dirty=True
    assert s.cache.due(s.app,160,40) and not s.cache.maintain(s.app,160,40)
    s.cache.dirty=False
    assert s.cache.due(s.app,80,40)
    s.cache.next_animation=s.now[0]+.01
    s.now[0]+=.02
    assert s.cache.due(s.app,160,40)
